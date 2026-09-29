from enum import Enum


class WordTypeQuery(str, Enum):
    parasite = 'parasite'
    commonly_mispronounced = 'commonly-mispronounced'


class SortingOrder(str, Enum):
    ascending = 'asc'
    descending = 'desc'
