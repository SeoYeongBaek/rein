from enum import IntEnum


class Verdict(IntEnum):
    """
    판정 우선순위 (숫자가 클수록 더 제한적이며 우선순위가 높음)
    충돌 시: DENY(4) > APPROVE(3) > RETRY(2) > ALLOW(1)
    """
    ALLOW = 1
    RETRY = 2
    APPROVE = 3
    DENY = 4

    def __str__(self):
        return self.name.lower()