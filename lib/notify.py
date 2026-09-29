"""(v0.2) 텔레그램 토큰이 URL 에 노출되지 않도록 예외 메시지에서 마스킹 (M2)."""
import re

TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def mask(text):
    return TOKEN_RE.sub("bot***", str(text))
