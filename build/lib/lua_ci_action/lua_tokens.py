from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    value: str
    line: int
    kind: str


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def long_bracket_end(text: str, offset: int) -> tuple[str, int] | None:
    if offset >= len(text) or text[offset] != "[":
        return None
    cursor = offset + 1
    while cursor < len(text) and text[cursor] == "=":
        cursor += 1
    if cursor >= len(text) or text[cursor] != "[":
        return None
    equals = text[offset + 1 : cursor]
    return "]" + equals + "]", cursor + 1


def skip_long_bracket(text: str, offset: int, line: int) -> tuple[int, int] | None:
    opening = long_bracket_end(text, offset)
    if opening is None:
        return None
    closing, content_start = opening
    closing_at = text.find(closing, content_start)
    if closing_at < 0:
        raise ValueError(f"unterminated long bracket at line {line}")
    end = closing_at + len(closing)
    return end, line + text[offset:end].count("\n")


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    offset = 0
    line = 1
    while offset < len(text):
        character = text[offset]
        if character.isspace():
            if character == "\n":
                line += 1
            offset += 1
            continue
        if text.startswith("--", offset):
            long_comment = skip_long_bracket(text, offset + 2, line)
            if long_comment is not None:
                offset, line = long_comment
                continue
            newline = text.find("\n", offset + 2)
            if newline < 0:
                break
            offset = newline
            continue
        if character in {"'", '"'}:
            quote = character
            start_line = line
            offset += 1
            while offset < len(text):
                character = text[offset]
                if character == "\\":
                    if offset + 1 < len(text) and text[offset + 1] == "\n":
                        line += 1
                    offset += 2
                    continue
                if character == quote:
                    offset += 1
                    break
                if character == "\n":
                    line += 1
                offset += 1
            else:
                raise ValueError(f"unterminated string at line {start_line}")
            continue
        long_string = skip_long_bracket(text, offset, line)
        if long_string is not None:
            offset, line = long_string
            continue
        match = IDENTIFIER.match(text, offset)
        if match is not None:
            tokens.append(Token(value=match.group(0), line=line, kind="identifier"))
            offset = match.end()
            continue
        if text.startswith("...", offset):
            tokens.append(Token(value="...", line=line, kind="symbol"))
            offset += 3
            continue
        two_character = text[offset : offset + 2]
        if two_character in {"==", "~=", "<=", ">=", "..", "::"}:
            tokens.append(Token(value=two_character, line=line, kind="symbol"))
            offset += 2
            continue
        tokens.append(Token(value=character, line=line, kind="symbol"))
        offset += 1
    return tokens
