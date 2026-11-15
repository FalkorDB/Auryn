"""Unit tests for catalog helpers that need no live Oracle connection."""

from __future__ import annotations

import pytest

from auryn.ingestors.plsql import catalog


@pytest.mark.parametrize(
    "source",
    [
        # Reconstructed the way source_text builds it ("CREATE OR REPLACE " + ALL_SOURCE):
        'CREATE OR REPLACE PACKAGE BODY "X" wrapped\na000000\nb2 ...',
        "CREATE OR REPLACE FUNCTION secret_double wrapped\na000000\nabcd",
        "CREATE OR REPLACE PROCEDURE do_secret WRAPPED\n0\n1",  # keyword is case-insensitive
        "\n\n  PACKAGE BODY pkg wrapped\n1234",  # leading blank lines ignored
    ],
)
def test_is_wrapped_source_detects_obfuscated_units(source):
    assert catalog.is_wrapped_source(source) is True


@pytest.mark.parametrize(
    "source",
    [
        None,
        "",
        "   \n  ",
        "CREATE OR REPLACE PACKAGE BODY greetings_pkg IS\n  PROCEDURE greet_all IS ...",
        "CREATE OR REPLACE FUNCTION double_it(p_n IN NUMBER) RETURN NUMBER IS\nBEGIN\n  RETURN p_n * 2;\nEND;",
        # The word "wrapped" appearing only in the body must NOT trip detection -- only
        # the first header line is inspected.
        "CREATE OR REPLACE PROCEDURE p IS\nBEGIN\n  -- this value is wrapped\n  NULL;\nEND;",
        "BEGIN\n  :NEW.last_greeted := SYSDATE;\nEND;",  # a plain trigger body
    ],
)
def test_is_wrapped_source_passes_readable_units(source):
    assert catalog.is_wrapped_source(source) is False
