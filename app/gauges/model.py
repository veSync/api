# -*- coding: utf-8 -*-
from multicall import Call, Multicall
from walrus import Model, TextField, IntegerField, FloatField, HashField
from web3.constants import ADDRESS_ZERO

from app.settings import (
    LOGGER, CACHE, VOTER_ADDRESS,
    DEFAULT_TOKEN_ADDRESS, VE_ADDRESS
)
from app.assets import Token


class Gauge(Model):
    """Gauge model."""
    __database__ = CACHE

    DEFAULT_DECIMALS = 18
    DAY_IN_SECONDS = 24 * 60 * 60
    CACHER = CACHE.cache()

    address = TextField(primary_key=True)
    decimals = IntegerField(default=DEFAULT_DECIMALS)
    total_supply = FloatField()
    bribe_address = TextField(index=True)
    fees_address = TextField(index=True)
    wrapped_bribe_address = TextField(index=True)
    # Per epoch...
    reward = FloatField()

    # Bribes in the form of `token_address => token_amount`...
    rewards = HashField()
    # Pair Fees (Internal)
    fees = FloatField(default=0.0)
    # Total Bribes Value
    tbv = FloatField(default=0.0)
    # Voting APR
    votes = FloatField(default=0.0)
    apr = FloatField(default=0.0)

    # TODO: Backwards compat. Remove once no longer needed...
    bribeAddress = TextField()
    feesAddress = TextField()
    totalSupply = FloatField()

    @classmethod
    def find(cls, address):
        """Loads a gauge from cache, of from chain if not found."""
        if address is None:
            return None

        try:
            return cls.load(address.lower())
        except KeyError:
            return cls.from_chain(address.lower())

    @classmethod
    def from_chain(cls, address):
        """Fetches pair/pool gauge data from chain."""
        address = address.lower()

        pair_gauge_multi = Multicall([
            Call(
                address,
                'totalSupply()(uint256)',
                [['total_supply', None]]
            ),
            Call(
                address,
                ['rewardRate(address)(uint256)', DEFAULT_TOKEN_ADDRESS],
                [['reward_rate', None]]
            ),
            Call(
                VOTER_ADDRESS,
                ['external_bribes(address)(address)', address],
                [['bribe_address', None]]
            ),
            Call(
                VOTER_ADDRESS,
                ['internal_bribes(address)(address)', address],
                [['fees_address', None]]
            )
        ])

        data = pair_gauge_multi()
        data['decimals'] = cls.DEFAULT_DECIMALS
        data['total_supply'] = data['total_supply'] / data['decimals']

        token = Token.find(DEFAULT_TOKEN_ADDRESS)
        data['reward'] = (
            data['reward_rate'] / 10**token.decimals * cls.DAY_IN_SECONDS
        )

        # TODO: Remove once no longer needed...
        data['bribeAddress'] = data['bribe_address']
        data['feesAddress'] = data['fees_address']
        data['totalSupply'] = data['total_supply']
        data['wrapped_bribe_address'] = data['bribe_address']

        # Cleanup old data
        cls.query_delete(cls.address == address.lower())

        gauge = cls.create(address=address, **data)
        LOGGER.debug('Fetched %s:%s.', cls.__name__, address)

        if data.get('wrapped_bribe_address') not in (ADDRESS_ZERO, None):
            cls._fetch_external_rewards(gauge)

        cls._fetch_internal_rewards(gauge)
        cls._update_apr(gauge)

        return gauge

    @classmethod
    @CACHER.cached(timeout=(1 * DAY_IN_SECONDS))
    def rebase_apr(cls):
        minter_address = Call(VOTER_ADDRESS, 'minter()(address)')()
        weekly = Call(minter_address, 'weekly_emission()(uint256)')()
        supply = Call(VE_ADDRESS, 'supply()(uint256)')()
        growth = Call(
            minter_address,
            ['calculate_growth(uint256)(uint256)', weekly]
        )()

        return ((growth * 52) / supply) * 100

    @classmethod
    def _update_apr(cls, gauge):
        """Updates the voting apr for the gauge."""
        # Avoid circular import...
        from app.pairs.model import Pair

        pair = Pair.get(Pair.gauge_address == gauge.address)

        votes = Call(
            VOTER_ADDRESS,
            ['weights(address)(uint256)', pair.address]
        )()

        token = Token.find(DEFAULT_TOKEN_ADDRESS)
        votes = votes / 10**token.decimals

        gauge.apr = cls.rebase_apr()

        if token.price and votes * token.price > 0:
            gauge.votes = votes
            gauge.apr += ((gauge.tbv * 52) / (votes * token.price)) * 100
            gauge.save()

    @classmethod
    def _fetch_external_rewards(cls, gauge):
        """Fetches gauge external rewards (bribes) data from chain."""
        tokens_len = Call(
            gauge.wrapped_bribe_address,
            'rewardsListLength()(uint256)'
        )()

        reward_calls = []

        for idx in range(0, tokens_len):
            bribe_token_address = Call(
                gauge.wrapped_bribe_address,
                ['rewards(uint256)(address)', idx]
            )()

            reward_calls.append(
                Call(
                    gauge.wrapped_bribe_address,
                    ['left(address)(uint256)', bribe_token_address],
                    [[bribe_token_address, None]]
                )
            )

        data = Multicall(reward_calls)()

        for (bribe_token_address, amount) in data.items():
            # Refresh cache if needed...
            token = Token.find(bribe_token_address)

            gauge.rewards[token.address] = amount / 10**token.decimals

            if token.price:
                gauge.tbv += amount / 10**token.decimals * token.price

            LOGGER.debug(
                'Fetched %s:%s reward %s:%s.',
                cls.__name__,
                gauge.address,
                bribe_token_address,
                gauge.rewards[token.address]
            )

        gauge.save()

    @classmethod
    def _fetch_internal_rewards(cls, gauge):
        """Fetches gauge internal rewards (fees) data from chain."""
        # Avoid circular import...
        from app.pairs.model import Pair

        pair = Pair.get(Pair.gauge_address == gauge.address)
        pair_fees_address = Call(pair.address, 'fees()(address)')()

        fees_data = Multicall([
            Call(
                gauge.address,
                'fees0()(uint256)',
                [['fees0', None]]
            ),
            Call(
                gauge.address,
                'fees1()(uint256)',
                [['fees1', None]]
            )
        ])()

        fees = [
            [pair.token0_address, fees_data['fees0']],
            [pair.token1_address, fees_data['fees1']]
        ]

        for (token_address, fee) in fees:
            token = Token.find(token_address)

            if gauge.rewards.get(token_address):
                gauge.rewards[token_address] = (
                    float(gauge.rewards[token_address]) + (
                        fee / 10**token.decimals
                    )
                )
            elif fee > 0:
                gauge.rewards[token_address] = fee / 10**token.decimals

            if token.price:
                gauge.tbv += fee / 10**token.decimals * token.price
                gauge.fees += fee / 10**token.decimals * token.price

            LOGGER.debug(
                'Fetched %s:%s fee reward %s:%s.',
                cls.__name__,
                gauge.address,
                token_address,
                gauge.rewards[token_address]
            )

        gauge.save()
