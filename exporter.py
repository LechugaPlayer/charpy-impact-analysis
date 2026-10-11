"""
exporter.py
===========
Exportación de resultados del Analizador de Ensayo Charpy en tres formatos:

  - PDF : reporte organizado (parámetros, resultados, gráficos), con
          reportlab (Platypus).
  - JPG : cada gráfico de matplotlib guardado como imagen independiente.
  - TXT : tabla de datos PROCESADOS (ángulo suavizado + derivadas +
          energías), separada por tabuladores, con un encabezado de
          metadata comentado con '#'. Distinta de la tabla cruda que ya
          exporta el propio Arduino con el comando 'd'.

No depende de la GUI: recibe objetos de engine.py (ResultadoAnalisis,
ParametrosFisicos) y, opcionalmente, un diccionario de Figuras de
matplotlib ya construidas (por ejemplo las mismas que arma gui.py).
"""

import os
import re
import unicodedata
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional

import numpy as np
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image as RLImage, PageBreak,
)

from engine import ResultadoAnalisis, ParametrosFisicos


class ExportError(Exception):
    """Error al generar cualquiera de los archivos de exportación."""
    pass


# ============================================================
# UTILIDADES INTERNAS
# ============================================================
def _slug(texto: str) -> str:
    """Convierte un nombre descriptivo (con acentos/espacios) en un nombre
    de archivo seguro, p. ej. 'Ángulo vs tiempo' -> 'angulo_vs_tiempo'."""
    normalizado = unicodedata.normalize("NFKD", texto)
    sin_acentos = "".join(c for c in normalizado if not unicodedata.combining(c))
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", sin_acentos).strip("_").lower()
    return slug or "grafico"


# ============================================================
# EXPORTACION A TXT (DATOS PROCESADOS)
# ============================================================
def exportar_datos_txt(
    resultado: ResultadoAnalisis,
    ruta: str,
    parametros: Optional[ParametrosFisicos] = None,
) -> None:
    """
    Exporta la tabla de datos PROCESADOS (spline suavizado, velocidad y
    aceleración angular, energías) a un .txt separado por tabuladores,
    listo para pegar en Excel/Sheets. Incluye metadata del ensayo como
    líneas comentadas con '#' al inicio.

    Si `resultado.energia_cinetica_j` es None (no se definió momento de
    inercia), se omiten las columnas de energía cinética/total/potencia
    en vez de rellenarlas con valores vacíos.
    """
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write("# Reporte de datos procesados - Ensayo Charpy\n")
            f.write(f"# Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

            if parametros is not None:
                inercia_txt = (
                    f"{parametros.momento_inercia:.6f} kg*m^2"
                    if parametros.momento_inercia is not None else "no definido"
                )
                f.write(
                    f"# Parametros: masa={parametros.masa_pendulo:.4f} kg, "
                    f"radio={parametros.radio_centro_masa:.4f} m, I={inercia_txt}\n"
                )

            confiable_txt = "confiable" if resultado.rebote_confiable else "ESTIMADO (no confiable, ver aviso en la app)"
            f.write(f"# Angulo inicial usado: {resultado.angulo_inicial_usado_grados:.2f} grados\n")
            f.write(f"# Angulo final (rebote) usado: {resultado.angulo_final_usado_grados:.2f} grados ({confiable_txt})\n")
            f.write(f"# Energia absorbida bruta: {resultado.energia_absorbida_bruta_j:.4f} J\n")
            f.write(f"# Energia absorbida neta:  {resultado.energia_absorbida_neta_j:.4f} J\n")
            f.write("#\n")

            hay_cinetica = resultado.energia_cinetica_j is not None
            columnas = [
                "Tiempo_ms", "Angulo_grados", "Velocidad_grados_s",
                "Aceleracion_grados_s2", "Energia_potencial_J",
            ]
            if hay_cinetica:
                columnas += ["Energia_cinetica_J", "Energia_total_J", "Potencia_W"]
            f.write("\t".join(columnas) + "\n")

            t_ms = resultado.tiempo_s * 1000.0
            vel_deg_s = np.degrees(resultado.velocidad_angular_rad_s)
            acel_deg_s2 = np.degrees(resultado.aceleracion_angular_rad_s2)

            for i in range(len(t_ms)):
                fila = [
                    f"{t_ms[i]:.3f}",
                    f"{resultado.angulo_suavizado_grados[i]:.3f}",
                    f"{vel_deg_s[i]:.3f}",
                    f"{acel_deg_s2[i]:.3f}",
                    f"{resultado.energia_potencial_j[i]:.5f}",
                ]
                if hay_cinetica:
                    fila += [
                        f"{resultado.energia_cinetica_j[i]:.5f}",
                        f"{resultado.energia_mecanica_total_j[i]:.5f}",
                        f"{resultado.potencia_instantanea_w[i]:.5f}",
                    ]
                f.write("\t".join(fila) + "\n")

    except OSError as e:
        raise ExportError(f"No se pudo escribir el archivo de datos '{ruta}': {e}")


# ============================================================
# EXPORTACION A JPG (GRAFICOS)
# ============================================================
def exportar_graficos_jpg(
    figuras: Dict[str, "object"],
    carpeta_destino: str,
    prefijo: str = "charpy",
    dpi: int = 200,
) -> List[str]:
    """
    Guarda cada figura de matplotlib (matplotlib.figure.Figure) como un
    .jpg independiente dentro de carpeta_destino.

    figuras: dict {nombre_descriptivo: Figure}, p. ej.
             {"Ángulo vs tiempo": fig_angulo, "Velocidad": fig_vel, ...}

    Devuelve la lista de rutas de archivo escritas.
    """
    if not os.path.isdir(carpeta_destino):
        raise ExportError(f"La carpeta de destino no existe: {carpeta_destino}")

    rutas_escritas = []
    for nombre, figura in figuras.items():
        ruta = os.path.join(carpeta_destino, f"{prefijo}_{_slug(nombre)}.jpg")
        try:
            # facecolor="white" es importante: JPG no soporta transparencia,
            # y sin esto el fondo puede salir negro en vez de blanco.
            figura.savefig(ruta, format="jpg", dpi=dpi, facecolor="white", bbox_inches="tight")
        except Exception as e:
            raise ExportError(f"No se pudo guardar el grafico '{nombre}': {e}")
        rutas_escritas.append(ruta)

    return rutas_escritas


# ============================================================
# EXPORTACION A PDF (REPORTE)
# ============================================================
def _figura_a_flowable_imagen(figura, ancho_disponible_pt: float, dpi: int = 150) -> RLImage:
    """Convierte una Figure de matplotlib en un flowable Image de reportlab,
    respetando la relacion de aspecto original para no deformarla."""
    buffer = BytesIO()
    figura.savefig(buffer, format="png", dpi=dpi, facecolor="white", bbox_inches="tight")
    buffer.seek(0)

    ancho_px, alto_px = PILImage.open(buffer).size
    buffer.seek(0)

    alto_pt = ancho_disponible_pt * (alto_px / ancho_px)
    return RLImage(buffer, width=ancho_disponible_pt, height=alto_pt)


def _estilos_reporte():
    base = getSampleStyleSheet()
    base.add(ParagraphStyle(
        name="AvisoRebote", parent=base["Normal"],
        textColor=colors.HexColor("#B35C00"), backColor=colors.HexColor("#FFF3E0"),
        borderColor=colors.HexColor("#D68910"), borderWidth=0.75,
        borderPadding=8, spaceBefore=6, spaceAfter=12,
    ))
    base.add(ParagraphStyle(
        name="Subtitulo", parent=base["Normal"],
        textColor=colors.HexColor("#555555"), fontSize=10, spaceAfter=14,
    ))
    return base


def exportar_reporte_pdf(
    resultado: ResultadoAnalisis,
    parametros: ParametrosFisicos,
    ruta_pdf: str,
    figuras: Optional[Dict[str, "object"]] = None,
    nombre_ensayo: Optional[str] = None,
) -> None:
    """
    Genera un reporte PDF organizado con: parámetros del péndulo,
    resultados de energía, aviso de confiabilidad (si corresponde) y,
    si se pasan `figuras` (dict {nombre: Figure de matplotlib}), cada
    gráfico en una página aparte.

    NOTA TECNICA: se evitan deliberadamente los caracteres Unicode de
    superíndice (², ³) porque las fuentes base de reportlab no traen esos
    glifos y se renderian como recuadros negros; se usa notación "^2" en
    su lugar.
    """
    try:
        doc = SimpleDocTemplate(
            ruta_pdf, pagesize=A4,
            topMargin=2 * cm, bottomMargin=2 * cm, leftMargin=2 * cm, rightMargin=2 * cm,
        )
    except Exception as e:
        raise ExportError(f"No se pudo crear el archivo PDF '{ruta_pdf}': {e}")

    estilos = _estilos_reporte()
    story = []

    # --- Encabezado ---
    story.append(Paragraph("Reporte de Ensayo Charpy", estilos["Title"]))
    subtitulo = nombre_ensayo if nombre_ensayo else "Péndulo casero - Arduino + encoder E38S6G5-600B-G24N"
    story.append(Paragraph(subtitulo, estilos["Subtitulo"]))
    story.append(Paragraph(
        f"Generado el {datetime.now().strftime('%d/%m/%Y a las %H:%M')}",
        estilos["Normal"],
    ))
    story.append(Spacer(1, 16))

    # --- Tabla de parametros ---
    story.append(Paragraph("Parámetros del péndulo", estilos["Heading2"]))
    inercia_txt = (
        f"{parametros.momento_inercia:.6f} kg*m^2"
        if parametros.momento_inercia is not None else "no definido"
    )
    tabla_parametros = Table(
        [
            ["Masa del péndulo", f"{parametros.masa_pendulo:.4f} kg"],
            ["Radio eje - centro de masa", f"{parametros.radio_centro_masa:.4f} m"],
            ["Momento de inercia (I)", inercia_txt],
            ["Aceleración de la gravedad (g)", f"{parametros.g:.4f} m/s^2"],
        ],
        colWidths=[9 * cm, 7 * cm],
    )
    tabla_parametros.setStyle(_estilo_tabla())
    story.append(tabla_parametros)
    story.append(Spacer(1, 16))

    # --- Tabla de resultados ---
    story.append(Paragraph("Resultados", estilos["Heading2"]))
    tabla_resultados = Table(
        [
            ["Ángulo inicial usado", f"{resultado.angulo_inicial_usado_grados:.2f} grados"],
            ["Ángulo de rebote usado", f"{resultado.angulo_final_usado_grados:.2f} grados"],
            ["Energía absorbida (bruta)", f"{resultado.energia_absorbida_bruta_j:.4f} J"],
            ["Energía absorbida (neta)", f"{resultado.energia_absorbida_neta_j:.4f} J"],
        ],
        colWidths=[9 * cm, 7 * cm],
    )
    tabla_resultados.setStyle(_estilo_tabla())
    story.append(tabla_resultados)
    story.append(Spacer(1, 10))

    if not resultado.rebote_confiable:
        story.append(Paragraph(
            "<b>Aviso:</b> el ángulo de rebote no se pudo confirmar con los datos "
            "capturados (posible corte del buffer del Arduino a 100 muestras). "
            "El valor de energía mostrado puede estar SOBRESTIMADO. Para un "
            "resultado exacto, volvé a capturar el archivo incluyendo la salida "
            "del comando 'e' del Arduino (bloque RESULTADO).",
            estilos["AvisoRebote"],
        ))

    # --- Graficos, uno por pagina ---
    if figuras:
        ancho_disponible = doc.width  # ancho util de la pagina, en puntos
        for nombre, figura in figuras.items():
            story.append(PageBreak())
            story.append(Paragraph(nombre, estilos["Heading2"]))
            try:
                imagen = _figura_a_flowable_imagen(figura, ancho_disponible)
            except Exception as e:
                story.append(Paragraph(f"(No se pudo incluir este gráfico: {e})", estilos["Normal"]))
                continue
            story.append(imagen)

    try:
        doc.build(story)
    except Exception as e:
        raise ExportError(f"No se pudo generar el PDF: {e}")


def _estilo_tabla() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EFEFEF")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#333333")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ])
