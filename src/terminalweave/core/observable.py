from __future__ import annotations
from typing import Any, Callable

Listener = Callable[[str, Any], None]


class Observable:
    """Minimal synchronous notification mixin.

    Subscribers receive ``(event, payload)`` after the publishing object has
    already applied the change.  Listeners are plain callables so embedding
    applications can bridge them onto their own event bus.
    """

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        if listener not in self._listeners:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def notify(self, event: str, payload: Any) -> None:
        for listener in list(self._listeners):
            listener(event, payload)
