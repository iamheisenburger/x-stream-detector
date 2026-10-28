"""Detect new posts from chosen X accounts within seconds, through X's official filtered stream."""
from .budget import BudgetClosed, ReadLedger
from .client import CreditsDepleted, RateLimited, StreamError, XStreamClient
from .events import parse_stream_event
from .rules import RuleSpec, canonical_rule_specs
from .runner import Detector
from .settings import Settings, load

__all__ = [
    "BudgetClosed",
    "CreditsDepleted",
    "Detector",
    "RateLimited",
    "ReadLedger",
    "RuleSpec",
    "Settings",
    "StreamError",
    "XStreamClient",
    "canonical_rule_specs",
    "load",
    "parse_stream_event",
]
