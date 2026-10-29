"""PDF -> texto por página (capítulo 4).

Camada de texto quando o PDF tem; OCR só nas páginas que não têm.
Cada página carrega a origem, para a tela de conferência mostrar de onde
veio cada valor.
"""
import os
from dataclasses import dataclass

import pdfplumber


@dataclass
class Pagina:
    numero: int
    texto: str
    origem: str  # "texto" ou "ocr"


def ocr_tesseract(pagina, dpi: int = 300, idioma: str | None = None) -> str:
    import pytesseract
    imagem = pagina.to_image(resolution=dpi).original
    idioma = idioma or os.getenv("OCR_IDIOMA", "por")
    return pytesseract.image_to_string(imagem, lang=idioma)


def extrair_paginas(caminho: str, ocr=ocr_tesseract,
                    minimo_caracteres: int = 20) -> list[Pagina]:
    paginas = []
    with pdfplumber.open(caminho) as pdf:
        for numero, pagina in enumerate(pdf.pages, start=1):
            texto = pagina.extract_text() or ""
            if len(texto.strip()) >= minimo_caracteres:
                paginas.append(Pagina(numero, texto, "texto"))
            else:
                paginas.append(Pagina(numero, ocr(pagina), "ocr"))
    return paginas
