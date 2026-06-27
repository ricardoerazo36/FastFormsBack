"""US-19 — Normalización de una transcripción hablada a un código alfanumérico.

Dictar códigos cortos (p. ej. `A7X9K`) es propenso a error, así que el valor
está en esta capa: convertir lo que Whisper transcribió ("a siete equis nueve
ka", "A 7 X 9 K", "a-7-x-9-k"…) a una cadena `[A-Z0-9]` en mayúsculas.
"""

import re
import unicodedata

# Palabras → dígito (español e inglés básico).
_NUMBER_WORDS = {
    "cero": "0", "zero": "0",
    "uno": "1", "una": "1", "un": "1", "one": "1",
    "dos": "2", "two": "2",
    "tres": "3", "three": "3",
    "cuatro": "4", "four": "4",
    "cinco": "5", "five": "5",
    "seis": "6", "six": "6",
    "siete": "7", "seven": "7",
    "ocho": "8", "eight": "8",
    "nueve": "9", "nine": "9",
}

# Palabras que nombran una letra → letra. Cubrimos la fonética típica en
# español ("ka" → K, "equis" → X, "doble u" → W, "i griega" → Y) y deletreos
# frecuentes.
_LETTER_WORDS = {
    "a": "A", "be": "B", "ce": "C", "de": "D", "e": "E", "efe": "F",
    "ge": "G", "hache": "H", "i": "I", "jota": "J", "ka": "K", "ele": "L",
    "eme": "M", "ene": "N", "enie": "N", "o": "O", "pe": "P", "cu": "Q",
    "ku": "Q", "ere": "R", "erre": "R", "ese": "S", "te": "T", "u": "U",
    "uve": "V", "ve": "V", "doble": "W", "equis": "X",
    "ygriega": "Y", "igriega": "Y", "ye": "Y", "zeta": "Z", "ceta": "Z",
}

# Frases de varias palabras que mapean a una sola letra (se resuelven antes
# del split por palabra).
_LETTER_PHRASES = [
    ("doble u", "W"),
    ("doble ve", "W"),
    ("i griega", "Y"),
    ("y griega", "Y"),
]


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize_spoken_code(text: str) -> str:
    """Convierte una transcripción a un código `[A-Z0-9]` en mayúsculas.

    Estrategia:
    1. Normaliza acentos y minúsculas; resuelve frases de varias palabras.
    2. Tokeniza por palabras/caracteres y mapea números y letras dictadas.
    3. Como respaldo, conserva cualquier `[a-z0-9]` suelto del texto original
       (cubre cuando Whisper ya devolvió "A7X9K" pegado).
    """
    if not text:
        return ""

    lowered = _strip_accents(text).lower()
    for phrase, letter in _LETTER_PHRASES:
        lowered = lowered.replace(phrase, f" {letter.lower()} ")

    # Separamos por cualquier cosa que no sea alfanumérico.
    tokens = re.split(r"[^a-z0-9]+", lowered)

    out: list[str] = []
    for token in tokens:
        if not token:
            continue
        if token in _NUMBER_WORDS:
            out.append(_NUMBER_WORDS[token])
        elif token in _LETTER_WORDS:
            out.append(_LETTER_WORDS[token])
        elif len(token) == 1 and token.isalnum():
            # Letra o dígito suelto ("a", "7").
            out.append(token.upper())
        else:
            # Token alfanumérico ya "pegado" (p. ej. "a7x9k" o "x9").
            # Conservamos solo sus caracteres alfanuméricos.
            out.append(re.sub(r"[^a-z0-9]", "", token).upper())

    return "".join(out)
