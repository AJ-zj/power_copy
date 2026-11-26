from typing import Protocol


class Cleaner(Protocol):
    name: str

    def clean(self, text: str) -> str:
        ...
