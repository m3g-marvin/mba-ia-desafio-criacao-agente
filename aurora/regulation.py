"""Busca local por capítulo e artigo; nunca devolve o documento inteiro."""

import re
import unicodedata
from pathlib import Path


def normalize(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c)
    )


ALIASES = {
    "III": ("silencio", "barulho", "ruido", "sossego"),
    "IV": ("piscina", "nadar", "natacao", "deck"),
    "V": ("academia", "brinquedoteca", "playground"),
    "VI": ("salao", "churrasqueira", "quadra", "reserva"),
    "VII": ("portaria", "seguranca", "visitante", "entrega", "entrada"),
    "VIII": ("animais", "animal", "cachorro", "gato", "pet"),
    "IX": ("mudanca",),
    "X": ("obra", "reforma"),
    "XI": ("garagem", "veiculo", "carro", "vaga"),
    "XII": ("lixo", "reciclagem", "residuo", "coleta"),
    "XIII": ("infracao", "multa", "penalidade", "advertencia"),
    "II": ("direito", "dever", "fachada", "sacada"),
    "I": ("administracao", "sindico", "convencao"),
    "XIV": ("vigencia", "disposicoes finais"),
}
STOP = {
    "a",
    "o",
    "os",
    "as",
    "um",
    "uma",
    "de",
    "do",
    "da",
    "dos",
    "das",
    "em",
    "no",
    "na",
    "nos",
    "nas",
    "que",
    "e",
    "ao",
    "aos",
    "para",
    "por",
    "como",
    "qual",
    "quais",
    "ate",
    "com",
    "se",
    "meu",
    "minha",
    "sobre",
    "funciona",
}


class Regulation:
    def __init__(self, path: Path):
        # The complete file exists only in server memory, outside prompts/events.
        self.chapters = []
        for chunk in re.split(r"(?m)^## ", path.read_text(encoding="utf-8"))[1:]:
            title, body = chunk.split("\n", 1)
            number = re.search(r"Capítulo ([IVX]+):", title).group(1)
            articles = [s.strip() for s in re.split(r"(?=\*\*Art\. )", body) if s.strip()]
            self.chapters.append((number, title, articles))

    def search(self, question: str) -> dict:
        q = normalize(question)
        tokens = set(re.findall(r"[a-z]+", q)) - STOP

        def hits(aliases):
            return sum(any(w.startswith(alias) for w in tokens) for alias in aliases)

        ranked = sorted(self.chapters, key=lambda c: hits(ALIASES[c[0]]), reverse=True)
        if not hits(ALIASES[ranked[0][0]]):
            return {
                "status": "especifique_assunto",
                "mensagem": "Informe o assunto da dúvida sobre o regulamento.",
            }
        _, title, articles = ranked[0]

        def score(article):
            words = set(re.findall(r"[a-z]+", normalize(article)))
            return sum(
                any(w.startswith(t) or t.startswith(w) for w in words if len(w) > 2) for t in tokens
            )

        selected = sorted(articles, key=score, reverse=True)[:2]
        return {"fonte": "dados/regulamento.md", "capitulo": title, "trechos": selected}
