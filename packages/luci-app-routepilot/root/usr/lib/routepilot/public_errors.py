"""Explicitly safe control errors; never expose arbitrary exception text."""


class ControlError(ValueError):
    pass
