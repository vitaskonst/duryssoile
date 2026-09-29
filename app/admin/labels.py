"""Kazakh labels for the admin page.

The admin interface is Kazakh-only -- there is no language switch and no
fallback locale. Strings that appear once live at their use site; the ones
below are shared between the router and the templates.

The two type labels match the ones the public clients show, so the admin page
and the clients name the same thing the same way.
"""

from ..models import WordType

TYPE_LABELS: dict[WordType, str] = {
    WordType.parasite: 'Бөгде сөздер',
    WordType.commonly_mispronounced: 'Жиі қате айтылатын сөздер',
}

# Singular forms, for headings and buttons that concern one word.
TYPE_LABELS_SINGULAR: dict[WordType, str] = {
    WordType.parasite: 'бөгде сөз',
    WordType.commonly_mispronounced: 'жиі қате айтылатын сөз',
}

# Only parasite words carry correct versions: in the source data every
# `correctVersions` entry belongs to a parasite word, and the public API omits
# the key entirely for the other type. The admin page follows suit -- it hides
# the editor for commonly-mispronounced words, and the router refuses to store
# correct versions against them, so the API shape stays as it was in 2023.
TYPES_WITH_CORRECT_VERSIONS = frozenset({WordType.parasite})


def type_label(word_type: WordType) -> str:
    return TYPE_LABELS[word_type]


def supports_correct_versions(word_type: WordType) -> bool:
    return word_type in TYPES_WITH_CORRECT_VERSIONS
