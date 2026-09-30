from .mock import MockSource
from .serpapi import SerpApiSource
from .travelpayouts import TravelpayoutsSource

DISCOVERY = {"travelpayouts": TravelpayoutsSource, "mock": MockSource}
VERIFY = {"serpapi": SerpApiSource}
