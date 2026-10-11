"""
engine.py
=========
Motor de cálculo matemático y parser de datos para el análisis del ensayo
Charpy casero (péndulo + encoder E38S6G5-600B-G24N + Arduino Uno).

------------------------------------------------------------------------
FORMATO DE ARCHIVO DE ENTRADA (generado por el sketch de Arduino)
------------------------------------------------------------------------
El único bloque del firmware que genera una tabla de datos tiempo/ángulo
es `imprimirTablaDatos()`, invocado con el comando serie 'd'. Su salida
por Serial.println() es exactamente:

    ===== TABLA DE DATOS (copiar y pegar en Excel/Sheets) =====
    Tiempo_us	Angulo_grados
    148	41.85
    3652	41.40
    7160	40.65
    ...
    ====== FIN DE TABLA ======
    Total de puntos: 187

Detalles relevantes extraídos del firmware (charpy_final_1/charpy_final_1.ino):
  - Separador de columnas: TABULADOR ('\t').
  - Columna 1 (Tiempo_us): entero, microsegundos transcurridos desde que
    el firmware detecta la suelta del péndulo (no desde que se presiona
    's'). NO es un timestamp absoluto: se reconstruye acumulando los
    deltas entre muestras, así que la primera fila tiene un valor chico
    pero no necesariamente 0. Este parser igual lo vuelve relativo a la
    primera muestra.
  - Columna 2 (Angulo_grados): float con 2 decimales (Serial.println(x,2)).
    Puede ser negativo (el péndulo puede cruzar el cero mecánico definido
    con el comando 'z' hacia el lado contrario). La resolución angular
    real del encoder es 360/(600*4) = 0.15°/cuenta, por lo que los
    valores son siempre múltiplos de 0.15, aunque se impriman con 2
    decimales.
  - Muestreo nominal: 1 muestra cada 3.5 ms (INTERVALO_MUESTREO_US =
    3500). Es un umbral mínimo evaluado en el loop, no un timer, así que
    el paso real es levemente irregular; cada fila trae su propio tiempo.
  - LÍMITE IMPORTANTE: el firmware solo guarda un máximo de
    MAX_MUESTRAS = 300 muestras por ensayo (limitación de RAM del Uno).
    Con 3.5 ms de paso, esto cubre como máximo ~1050 ms de movimiento
    desde la suelta. La captura se corta apenas se confirma el pico de
    rebote (el ángulo baja más de 1° desde el máximo), así que la tabla
    normalmente termina poco después de ese pico. Si el semiperiodo real
    del péndulo (liberación -> impacto -> rebote máximo) dura más de
    ~1050 ms, la tabla quedará
    TRUNCADA antes de alcanzar el ángulo máximo de rebote. El ángulo
    máximo que sí queda bien determinado (porque se calcula en cada
    iteración del loop, sin depender del buffer) se imprime aparte como
    "Angulo maximo de rebote" o "Angulo final (rebote)". Por eso este
    parser NO depende de que la tabla contenga el pico de rebote: el
    resultado "oficial" de energía (método de dos puntos, igual al
    firmware) se calcula preferentemente a partir de esos metadatos, y
    solo se usa el máximo dentro de la tabla como respaldo.

  - Si el usuario capturó también el resto del Monitor Serie (mensajes
    de bienvenida, barra de ángulo en vivo "Angulo: X deg [...]", bloque
    "RESULTADO DEL ENSAYO", calibración de fricción con 'c', etc.), ese
    texto puede estar mezclado en el mismo .txt. El parser debe ignorarlo
    de forma segura y, cuando sea posible, aprovechar esas líneas como
    metadata adicional (ángulo inicial, ángulo final, energías, pérdida
    por fricción) para pre-rellenar la interfaz.

  - Signo de los ángulos: la fórmula física de energía usa
    cos(theta), que es una función PAR (cos(x) == cos(-x)). Por lo tanto
    el signo de theta(t) es físicamente irrelevante para la energía
    potencial y no genera discontinuidades al derivar con el spline; no
    hace falta (ni conviene) tomar abs() sobre la señal continua.

------------------------------------------------------------------------
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.signal import savgol_filter, find_peaks


# ============================================================
# EXCEPCIONES
# ============================================================
class ArduinoDataError(Exception):
    """Error al parsear o validar el archivo de datos del Arduino."""
    pass


# ============================================================
# ESTRUCTURAS DE DATOS
# ============================================================
@dataclass
class ArduinoMetadata:
    """Metadata opcional extraída de líneas de texto del Monitor Serie
    (bloques '===== RESULTADO =====', calibración 'c', etc.), si están
    presentes en el mismo archivo. Todo es best-effort: puede quedar en
    None si el archivo solo contiene la tabla tiempo/ángulo."""
    angulo_inicial_grados: Optional[float] = None
    angulo_final_grados: Optional[float] = None
    energia_bruta_j: Optional[float] = None
    energia_neta_j: Optional[float] = None
    perdida_friccion_j: Optional[float] = None
    total_puntos_reportado: Optional[int] = None


@dataclass
class EnsayoData:
    """Datos crudos de un ensayo ya parseados y listos para el motor."""
    tiempo_s: np.ndarray        # tiempo en segundos, arranca en 0
    angulo_grados: np.ndarray   # ángulo en grados (con signo)
    metadata: ArduinoMetadata = field(default_factory=ArduinoMetadata)


@dataclass
class ParametrosFisicos:
    """Parámetros mecánicos del péndulo. Los nombres replican los del
    firmware (masa_pendulo, radio_centro_masa, g) para mantener
    consistencia entre el hardware y el software de análisis."""
    masa_pendulo: float          # kg
    radio_centro_masa: float     # m (eje de giro -> centro de masa)
    momento_inercia: Optional[float] = None  # kg*m^2, respecto al eje de giro
    g: float = 9.81              # m/s^2


@dataclass
class ResultadoAnalisis:
    """Resultado completo del análisis de un ensayo."""
    # --- Datos crudos (para graficar como puntos/dispersión) ---
    tiempo_crudo_s: np.ndarray
    angulo_crudo_grados: np.ndarray

    # --- Curvas evaluadas sobre la malla del spline (para graficar como línea) ---
    tiempo_s: np.ndarray
    angulo_suavizado_grados: np.ndarray
    velocidad_angular_rad_s: np.ndarray       # 1ra derivada del spline
    aceleracion_angular_rad_s2: np.ndarray    # 2da derivada del spline

    energia_potencial_j: np.ndarray
    energia_cinetica_j: Optional[np.ndarray]        # None si no hay momento de inercia
    energia_mecanica_total_j: Optional[np.ndarray]  # None si no hay momento de inercia
    potencia_instantanea_w: Optional[np.ndarray]    # None si no hay momento de inercia

    # --- Resultado escalar "tipo firmware" (método de dos puntos) ---
    energia_absorbida_bruta_j: float
    energia_absorbida_neta_j: float
    angulo_inicial_usado_grados: float  # angulo que se uso como "inicial" en el calculo
    angulo_final_usado_grados: float    # angulo que se uso como "rebote" en el calculo
    rebote_confiable: bool              # False si el rebote se estimo por fallback (ver advertencia)


# ============================================================
# PATRONES DE PARSEO
# ============================================================
# Línea de dato válida: entero (tiempo_us) + separador + float (angulo_grados).
# Acepta tabulador o espacios como separador, y coma o punto como decimal.
#
# FIX (detectado en pruebas de estres, jul-2026): la version anterior exigia
# un espacio obligatorio DESPUES del separador ';' o ',', por lo que lineas
# tipo "12000;45.06" (sin espacio) no matcheaban y se perdian en silencio --
# un caso real si el archivo se reexporta desde Excel en configuracion
# regional en espanol (usa ';' como separador CSV, sin espacio). Ahora el
# espacio es opcional cuando ya hubo un separador explicito.
_PATRON_LINEA_DATO = re.compile(
    r'^\s*(-?\d+)\s*(?:[\t;,]\s*|\s+)(-?\d+(?:[.,]\d+)?)\s*$'
)

# Patrones best-effort para metadata (no bloquean el parseo si no matchean).
_PATRONES_METADATA = {
    'angulo_inicial_grados': re.compile(r'Angulo inicial\s*:\s*(-?\d+(?:[.,]\d+)?)', re.IGNORECASE),
    'angulo_final_grados': re.compile(r'Angulo (?:final|maximo de rebote)[^:]*:\s*(-?\d+(?:[.,]\d+)?)', re.IGNORECASE),
    'energia_bruta_j': re.compile(r'Energia bruta[^:]*:\s*(-?\d+(?:[.,]\d+)?)', re.IGNORECASE),
    'energia_neta_j': re.compile(r'Energia absorbida NETA[^:]*:\s*(-?\d+(?:[.,]\d+)?)', re.IGNORECASE),
    'perdida_friccion_j': re.compile(r'Perdida por friccion[^:]*:\s*(-?\d+(?:[.,]\d+)?)', re.IGNORECASE),
    'total_puntos_reportado': re.compile(r'Total de puntos\s*:\s*(\d+)', re.IGNORECASE),
}


def _intentar_extraer_metadata(linea: str, metadata: ArduinoMetadata) -> None:
    """Intenta matchear la línea contra los patrones de metadata conocidos.
    No lanza excepciones: si nada matchea, simplemente no hace nada."""
    for campo, patron in _PATRONES_METADATA.items():
        m = patron.search(linea)
        if m:
            valor_str = m.group(1).replace(',', '.')
            valor = int(float(valor_str)) if campo == 'total_puntos_reportado' else float(valor_str)
            setattr(metadata, campo, valor)
            return


# ============================================================
# PARSER PRINCIPAL
# ============================================================
def read_arduino_data(filepath: str) -> EnsayoData:
    """
    Lee y parsea el archivo .txt generado por el comando 'd' del Arduino
    (ver imprimirTablaDatos() en el sketch).

    Es tolerante a:
      - Líneas vacías o con espacios sobrantes.
      - Encabezados, separadores "=====" y cualquier otro texto del
        Monitor Serie mezclado en el mismo archivo (bienvenida, barra de
        ángulo en vivo, bloques de RESULTADO, calibración con 'c').
      - Separador tabulador o espacios múltiples.
      - Coma decimal en vez de punto.
      - Finales de línea CRLF (Windows) o LF.
      - Líneas fuera de orden o con timestamps duplicados.

    De paso, intenta extraer metadata útil (ángulo inicial/final,
    energías, pérdida por fricción) de las líneas que no son parte de la
    tabla, si están presentes en el archivo.

    Lanza ArduinoDataError si no se encuentra ningún par (tiempo, ángulo)
    válido, o si hay menos de 4 puntos distintos (mínimo para CubicSpline).
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lineas = f.readlines()
    except FileNotFoundError:
        raise ArduinoDataError(f"No se encontró el archivo: {filepath}")
    except OSError as e:
        raise ArduinoDataError(f"No se pudo abrir el archivo '{filepath}': {e}")

    tiempos_us = []
    angulos = []
    metadata = ArduinoMetadata()

    for linea_cruda in lineas:
        linea = linea_cruda.strip()
        if not linea:
            continue

        m = _PATRON_LINEA_DATO.match(linea)
        if m:
            try:
                t_us = int(m.group(1))
                ang = float(m.group(2).replace(',', '.'))
            except ValueError:
                continue  # línea con formato numérico corrupto: se ignora, no se aborta
            tiempos_us.append(t_us)
            angulos.append(ang)
            continue

        # No es una línea de dato: puede ser metadata o ruido, se intenta igual
        _intentar_extraer_metadata(linea, metadata)

    if len(tiempos_us) == 0:
        raise ArduinoDataError(
            "No se encontraron datos válidos (tiempo, ángulo) en el archivo. "
            "Verifica que sea la salida del comando 'd' del Arduino "
            "(cabecera 'Tiempo_us<TAB>Angulo_grados')."
        )

    if (metadata.total_puntos_reportado is not None
            and metadata.total_puntos_reportado != len(tiempos_us)):
        print(
            f"[Aviso] El archivo reporta {metadata.total_puntos_reportado} puntos "
            f"pero se parsearon {len(tiempos_us)}. Se continúa con los datos disponibles."
        )

    t_arr = np.asarray(tiempos_us, dtype=np.float64)
    a_arr = np.asarray(angulos, dtype=np.float64)

    # Ordenar por tiempo (por si el archivo tiene líneas fuera de orden)
    orden = np.argsort(t_arr)
    t_arr = t_arr[orden]
    a_arr = a_arr[orden]

    # CubicSpline exige x estrictamente creciente: eliminar duplicados
    t_arr, indices_unicos = np.unique(t_arr, return_index=True)
    a_arr = a_arr[indices_unicos]

    if len(t_arr) < 4:
        raise ArduinoDataError(
            f"Se necesitan al menos 4 muestras distintas para interpolar "
            f"(se encontraron {len(t_arr)})."
        )

    tiempo_s = (t_arr - t_arr[0]) / 1_000_000.0  # us -> s, relativo al primer punto

    return EnsayoData(tiempo_s=tiempo_s, angulo_grados=a_arr, metadata=metadata)


# ============================================================
# UTILIDADES FÍSICAS INDEPENDIENTES
# ============================================================
def calcular_inercia_experimental(
    periodo_s: float,
    masa_pendulo: float,
    radio_centro_masa: float,
    g: float = 9.81,
) -> float:
    """
    Estima el momento de inercia I (kg*m^2) respecto al eje de giro, a
    partir del período de oscilación libre de PEQUEÑA amplitud (péndulo
    físico simple):

        T = 2*pi*sqrt(I / (m*g*R))   =>   I = T^2 * m * g * R / (4*pi^2)

    periodo_s: período de UNA oscilación completa (ida y vuelta), en
               segundos. Medir con cronómetro contando N oscilaciones
               libres (sin probeta) y dividiendo: T = t_total / N.
    masa_pendulo: masa total del péndulo (kg).
    radio_centro_masa: distancia del eje de giro al centro de masa (m).

    NOTA: esta fórmula asume amplitudes pequeñas (< ~15°). El péndulo de
    Charpy se suelta desde ángulos grandes (~90°), así que este valor es
    una APROXIMACIÓN. Para mayor precisión, medir el período con
    oscilaciones de poca amplitud (dejar caer el péndulo desde pocos
    grados, sin probeta) en vez de usar el swing real del ensayo.
    """
    if periodo_s <= 0 or masa_pendulo <= 0 or radio_centro_masa <= 0:
        raise ValueError("periodo_s, masa_pendulo y radio_centro_masa deben ser > 0.")
    return (periodo_s ** 2) * masa_pendulo * g * radio_centro_masa / (4 * np.pi ** 2)


def _rechazar_outliers_encoder(
    theta_deg: np.ndarray,
    umbral_grados: float = 8.0,
) -> np.ndarray:
    """
    Detecta y corrige muestras individuales anómalas ("glitches" de
    encoder: ruido eléctrico, pérdida puntual de sincronismo en la
    decodificación x4) ANTES de cualquier suavizado o estimación de
    rebote. Devuelve una copia corregida; nunca modifica el array
    original in-place.

    Por qué hace falta esto (detectado en pruebas de estrés, jul-2026):
    ------------------------------------------------------------------------
    Un solo glitch de gran magnitud (ej. 20-30°, producto de un pulso
    espurio en la interrupción del encoder) puede hacer que el estimador
    de rebote (`_estimar_angulo_rebote_desde_tabla`) confunda el pico
    espurio con un rebote real, y lo peor: con FALSA CONFIANZA
    (`confiable=True`, sin ningún aviso). Intentar hacer robusto el
    estimador de rebote *después* del hecho (exigiendo signo sostenido,
    magnitud mínima, etc.) resultó insuficiente en la práctica: cualquier
    heurística ahí termina o bien aceptando glitches grandes, o bien
    rechazando rebotes reales lentos (la velocidad angular es, por
    definición física, casi nula justo en el punto de retorno — el mismo
    patrón que un glitch pequeño). La solución robusta es atacar el
    glitch en el origen, antes de que llegue a ninguna etapa de análisis.

    Criterio de detección: una muestra i es un glitch si difiere de AMBOS
    vecinos inmediatos en más de 'umbral_grados', Y esos dos vecinos son
    consistentes entre sí (difieren poco uno del otro). Un cambio de
    tendencia real y sostenido no cumple este patrón: los vecinos tras un
    cambio genuino seguirían alejándose en la misma dirección, no
    coincidiendo entre sí como ocurre a los dos lados de un pico espurio
    de una sola muestra.

    La muestra detectada se reemplaza por el promedio de sus dos vecinos
    (interpolación lineal de 1 punto) — no se descarta la fila completa,
    para no alterar la grilla temporal del resto del pipeline.
    """
    theta_limpio = theta_deg.copy()
    for i in range(1, len(theta_deg) - 1):
        salto_izq = theta_deg[i] - theta_deg[i - 1]
        salto_der = theta_deg[i + 1] - theta_deg[i]
        vecinos_consistentes = abs(theta_deg[i + 1] - theta_deg[i - 1]) < umbral_grados / 2
        if (abs(salto_izq) > umbral_grados
                and abs(salto_der) > umbral_grados
                and vecinos_consistentes):
            theta_limpio[i] = (theta_deg[i - 1] + theta_deg[i + 1]) / 2.0
    return theta_limpio


def _estimar_angulo_rebote_desde_tabla(
    theta_deg: np.ndarray,
    prominencia_minima_grados: float = 0.5,
    ancho_minimo_muestras: int = 10,
) -> Tuple[float, bool]:
    """
    Estima el ángulo de rebote a partir de la tabla cruda, para usarse SOLO
    cuando no hay metadata disponible en el archivo. Devuelve
    (angulo_estimado, es_confiable).

    Por qué no basta con max(|theta|):
    ------------------------------------
    En un ensayo real, el ángulo de liberación (theta[0]) es, por
    definición física, siempre mayor en magnitud que el ángulo de rebote
    (si no hubiera pérdidas, serían iguales; con pérdidas, el rebote es
    menor). Por lo tanto max(|theta|) sobre toda la tabla devuelve casi
    siempre theta[0] (el ángulo inicial), NO el rebote. Ese fue un error
    de una primera versión de esta función, detectado y corregido.

    HISTORIAL DE ENDURECIMIENTO (jul-2026, pruebas de estrés):
    ------------------------------------------------------------------------
    Las primeras versiones de esta función buscaban "a mano" el primer
    cambio de signo sostenido en la derivada de theta(t). Se probaron
    sucesivamente: aceptar el primer cambio de signo (vulnerable a un solo
    glitch de encoder); exigir signo sostenido después del cambio
    (vulnerable al punto de RECUPERACIÓN de un glitch, que coincide por
    azar con la tendencia real); exigir signo sostenido a ambos lados más
    magnitud mínima local (vulnerable en el punto exacto de un rebote
    real, porque ahí la velocidad angular es casi nula por definición
    física — indistinguible localmente de ruido).

    La solución que sí resistió todos los casos de prueba (glitches de
    encoder, artefactos de borde de `savgol_filter`, Y rebotes reales
    lentos) es usar `scipy.signal.find_peaks` con dos criterios
    combinados sobre la señal YA LIMPIA de outliers
    (`_rechazar_outliers_encoder`):
      - `prominence`: cuánto se destaca el pico de su entorno.
      - `width`: cuántas muestras dura el pico — un glitch real dura 1
        muestra; un rebote físico, por la inercia del péndulo, se forma
        y disuelve a lo largo de decenas de muestras. Esta exigencia de
        ANCHO, no solo de altura, es la que finalmente distingue un pico
        espurio de uno físico, incluso cuando ambos superan el mismo
        umbral de prominencia.

    Se buscan picos tanto en theta(t) como en -theta(t), para detectar el
    rebote sin asumir de antemano si la liberación fue hacia el lado
    positivo o negativo.

    NOTA: esta función asume que `theta_deg` ya pasó por
    `_rechazar_outliers_encoder()` en `analizar()`, ANTES del suavizado
    opcional. El orden importa: aplicar el rechazo de outliers después de
    `savgol_filter` no funciona, porque el filtro esparce la influencia
    de un glitch entre varias muestras vecinas, y ya no se ve como un
    pico aislado de una sola muestra.

    Limitación conocida (importante para el usuario):
    ---------------------------------------------------
    Si el semiperiodo real del péndulo (liberación -> impacto -> pico de
    rebote) dura más que la ventana de captura del firmware, la tabla se
    corta ANTES de llegar al punto de retorno, y esta función no
    encontrará ningún pico que cumpla los criterios. En ese caso se
    devuelve el ÚLTIMO ángulo de la tabla como estimación, marcada como
    NO confiable. Esa estimación tiende a SOBRESTIMAR la energía
    absorbida (el péndulo probablemente sigue subiendo más allá del
    último dato capturado). En ese caso siempre es preferible usar la
    metadata del bloque RESULTADO del firmware (que calcula el ángulo
    máximo en tiempo real, sin depender del buffer de muestras).
    """
    picos_max, _ = find_peaks(
        theta_deg, prominence=prominencia_minima_grados, width=ancho_minimo_muestras
    )
    picos_min, _ = find_peaks(
        -theta_deg, prominence=prominencia_minima_grados, width=ancho_minimo_muestras
    )
    todos_los_picos = sorted(list(picos_max) + list(picos_min))

    if todos_los_picos:
        idx_rebote = todos_los_picos[0]  # el primero cronológicamente
        return float(theta_deg[idx_rebote]), True

    return float(theta_deg[-1]), False


def calcular_energia_absorbida_dos_puntos(
    angulo_inicial_deg: float,
    angulo_final_deg: float,
    masa_pendulo: float,
    radio_centro_masa: float,
    g: float = 9.81,
    perdida_friccion_j: float = 0.0,
) -> Tuple[float, float]:
    """
    Replica EXACTAMENTE la fórmula usada en el firmware
    (calcularYMostrarEnergia()):

        E_bruta = m * g * R * (cos(theta_final) - cos(theta_inicial))
        E_neta  = E_bruta - perdida_friccion_j   (clip a 0 si es negativa)

    Se aplica abs() sobre los ángulos por paridad con el firmware, aunque
    es matemáticamente irrelevante: cos(x) == cos(-x).

    Devuelve (energia_bruta_j, energia_neta_j).
    """
    theta_i = np.radians(abs(angulo_inicial_deg))
    theta_f = np.radians(abs(angulo_final_deg))
    e_bruta = masa_pendulo * g * radio_centro_masa * (np.cos(theta_f) - np.cos(theta_i))
    e_neta = e_bruta - perdida_friccion_j
    if e_neta < 0:
        e_neta = 0.0
    return float(e_bruta), float(e_neta)


# ============================================================
# MOTOR DE CÁLCULO PRINCIPAL
# ============================================================
class MotorCharpy:
    """
    Motor de cálculo físico para el ensayo Charpy.

    A partir de los parámetros mecánicos del péndulo (ParametrosFisicos) y
    de los datos crudos de un ensayo (EnsayoData), produce las curvas
    suavizadas de ángulo, velocidad angular, aceleración angular, energía
    potencial, energía cinética, energía mecánica total y potencia
    instantánea, usando un CubicSpline para suavizar y derivar.
    """

    def __init__(self, parametros: ParametrosFisicos):
        self._validar_parametros(parametros)
        self.parametros = parametros

    @staticmethod
    def _validar_parametros(p: ParametrosFisicos) -> None:
        if p.masa_pendulo <= 0:
            raise ValueError("La masa del péndulo debe ser mayor que cero.")
        if p.radio_centro_masa <= 0:
            raise ValueError("El radio (eje - centro de masa) debe ser mayor que cero.")
        if p.momento_inercia is not None and p.momento_inercia <= 0:
            raise ValueError("El momento de inercia, si se especifica, debe ser mayor que cero.")

    def analizar(
        self,
        datos: EnsayoData,
        perdida_friccion_j: float = 0.0,
        num_puntos_malla: Optional[int] = 400,
        ventana_suavizado: Optional[int] = None,
    ) -> ResultadoAnalisis:
        """
        Ejecuta el análisis completo sobre un EnsayoData ya parseado.

        num_puntos_malla: si se especifica, el spline se evalúa sobre una
        malla uniforme de ese tamaño (curvas suaves para graficar). Si es
        None, se evalúa exactamente sobre los instantes de muestreo
        originales (útil para exportar la tabla procesada punto a punto).

        ventana_suavizado: OPCIONAL. Tamaño de ventana (impar, >=5) de un
        filtro Savitzky-Golay aplicado al ángulo crudo ANTES de construir
        el spline. Por qué existe este parámetro:

          CubicSpline es un interpolador EXACTO: pasa por todos los puntos
          crudos, incluido el ruido de cuantización del encoder (0.15°/
          cuenta). Eso no es un problema para el ángulo en sí (se ve
          suave), pero al derivar dos veces para aceleración -y sobre
          todo para potencia, que depende de velocidad*aceleración- ese
          ruido de cuantización queda muy amplificado y las curvas salen
          con "dientes de sierra" de alta frecuencia sin significado
          físico. Si ves eso en tus gráficos de aceleración/potencia,
          probá con ventana_suavizado=9 u 11 (siempre impar). Por defecto
          (None) se preserva el comportamiento original: CubicSpline puro
          sobre los datos crudos, sin ningún pre-filtrado.
        """
        t = datos.tiempo_s
        theta_deg = datos.angulo_grados

        if len(t) < 4:
            raise ArduinoDataError(
                f"Se requieren al menos 4 muestras para interpolar (hay {len(t)})."
            )
        if np.any(np.diff(t) <= 0):
            raise ArduinoDataError("Los tiempos de muestreo deben ser estrictamente crecientes.")

        # Rechazo de outliers de encoder (glitches de una sola muestra) ANTES
        # de cualquier otro procesamiento -- ver docstring de
        # _rechazar_outliers_encoder(). Es intencional que esto se aplique
        # siempre, no solo cuando se pide ventana_suavizado: un glitch no
        # corregido corrompe por igual el spline (velocidad/aceleracion/
        # energia) y la estimacion de rebote por tabla.
        theta_deg = _rechazar_outliers_encoder(theta_deg)

        if ventana_suavizado is not None:
            if ventana_suavizado < 5 or ventana_suavizado % 2 == 0:
                raise ValueError("ventana_suavizado debe ser un entero impar >= 5.")
            if ventana_suavizado > len(theta_deg):
                raise ValueError(
                    f"ventana_suavizado ({ventana_suavizado}) no puede ser mayor que la "
                    f"cantidad de muestras ({len(theta_deg)})."
                )
            theta_deg = savgol_filter(theta_deg, window_length=ventana_suavizado, polyorder=3)

        theta_rad = np.radians(theta_deg)
        spline = CubicSpline(t, theta_rad)

        if num_puntos_malla:
            t_eval = np.linspace(t[0], t[-1], num_puntos_malla)
        else:
            t_eval = t

        theta_eval = spline(t_eval)        # rad
        omega_eval = spline(t_eval, 1)     # rad/s   (1ra derivada del spline)
        alpha_eval = spline(t_eval, 2)     # rad/s^2 (2da derivada del spline)

        m = self.parametros.masa_pendulo
        R = self.parametros.radio_centro_masa
        g = self.parametros.g
        I = self.parametros.momento_inercia

        # Energía potencial gravitatoria relativa al punto más bajo (theta=0).
        # cos() es par, así que el signo de theta no afecta este cálculo.
        energia_potencial = m * g * R * (1.0 - np.cos(theta_eval))

        energia_cinetica = None
        energia_total = None
        potencia = None
        if I is not None:
            energia_cinetica = 0.5 * I * omega_eval ** 2
            energia_total = energia_cinetica + energia_potencial
            # Potencia instantanea = dE/dt, calculada ANALITICAMENTE a partir
            # de las derivadas que ya nos da el spline, en vez de aplicar un
            # np.gradient() adicional sobre la energia (que seria una
            # diferenciacion numerica extra y amplificaria aun mas el ruido):
            #   dE/dt = d/dt[0.5*I*w^2 + m*g*R*(1-cos(theta))]
            #         = I*w*alpha + m*g*R*sin(theta)*w
            #         = w * (I*alpha + m*g*R*sin(theta))
            potencia = omega_eval * (I * alpha_eval + m * g * R * np.sin(theta_eval))

        # --- Energía absorbida, método de dos puntos (igual al firmware) ---
        # Preferir metadata del propio archivo (ángulo máx. de rebote calculado
        # en tiempo real por el firmware, no limitado por el buffer de 300
        # muestras); si no hay metadata, estimar a partir de la tabla cruda.
        if datos.metadata.angulo_inicial_grados is not None:
            angulo_inicial = datos.metadata.angulo_inicial_grados
        else:
            angulo_inicial = float(theta_deg[0])

        if datos.metadata.angulo_final_grados is not None:
            angulo_final_rebote = datos.metadata.angulo_final_grados
            rebote_confiable = True
        else:
            angulo_final_rebote, rebote_confiable = _estimar_angulo_rebote_desde_tabla(theta_deg)
            if not rebote_confiable:
                warnings.warn(
                    "No se encontro 'Angulo maximo de rebote' en la metadata del "
                    "archivo, y la tabla de datos no muestra un punto de retorno "
                    "(posible truncamiento por el limite de 300 muestras del "
                    "firmware). Se uso el ultimo angulo de la tabla como estimacion, "
                    "lo cual tiende a SOBRESTIMAR la energia absorbida. Para un "
                    "resultado exacto, incluye en el archivo la linea "
                    "'Angulo maximo de rebote' o 'Angulo final (rebote)' que "
                    "imprime el firmware.",
                    stacklevel=2,
                )

        e_bruta, e_neta = calcular_energia_absorbida_dos_puntos(
            angulo_inicial, angulo_final_rebote, m, R, g, perdida_friccion_j
        )

        return ResultadoAnalisis(
            tiempo_crudo_s=t,
            angulo_crudo_grados=theta_deg,
            tiempo_s=t_eval,
            angulo_suavizado_grados=np.degrees(theta_eval),
            velocidad_angular_rad_s=omega_eval,
            aceleracion_angular_rad_s2=alpha_eval,
            energia_potencial_j=energia_potencial,
            energia_cinetica_j=energia_cinetica,
            energia_mecanica_total_j=energia_total,
            potencia_instantanea_w=potencia,
            energia_absorbida_bruta_j=e_bruta,
            energia_absorbida_neta_j=e_neta,
            angulo_inicial_usado_grados=angulo_inicial,
            angulo_final_usado_grados=angulo_final_rebote,
            rebote_confiable=rebote_confiable,
        )


# ============================================================
# PRUEBA RÁPIDA POR LÍNEA DE COMANDOS
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python engine.py <ruta_al_archivo.txt> [masa_kg] [radio_m]")
        sys.exit(1)

    masa = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    radio = float(sys.argv[3]) if len(sys.argv) > 3 else 0.30

    try:
        datos = read_arduino_data(sys.argv[1])
    except ArduinoDataError as e:
        print(f"Error al leer datos: {e}")
        sys.exit(1)

    print(f"Muestras parseadas: {len(datos.tiempo_s)}")
    print(f"Duracion capturada en tabla: {datos.tiempo_s[-1] * 1000:.1f} ms")
    print(f"Angulo crudo[0]: {datos.angulo_grados[0]:.2f} grados")
    idx_max = int(np.argmax(np.abs(datos.angulo_grados)))
    print(f"Angulo crudo max |theta|: {datos.angulo_grados[idx_max]:.2f} grados (indice {idx_max})")
    print(f"Metadata detectada: {datos.metadata}")

    parametros = ParametrosFisicos(masa_pendulo=masa, radio_centro_masa=radio, momento_inercia=None)
    motor = MotorCharpy(parametros)
    resultado = motor.analizar(datos)
    print(f"Angulo final usado en el calculo: {resultado.angulo_final_usado_grados:.2f} grados "
          f"({'confiable' if resultado.rebote_confiable else 'ESTIMADO, revisar aviso arriba'})")
    print(f"Energia absorbida bruta: {resultado.energia_absorbida_bruta_j:.4f} J")
    print(f"Energia absorbida neta:  {resultado.energia_absorbida_neta_j:.4f} J")
