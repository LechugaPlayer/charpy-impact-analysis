# Analizador de Ensayo Charpy

Aplicación de escritorio para procesar los datos del péndulo de Charpy
casero (Arduino Uno + encoder E38S6G5-600B-G24N + OLED SSD1306).

## Estructura del proyecto

```
charpy_analizador/
├── engine.py    # Parser del .txt del Arduino + motor de cálculo físico
├── gui.py       # Interfaz gráfica (CustomTkinter + Matplotlib)
├── exporter.py  # Exportación a PDF (reporte), JPG (gráficos), TXT (datos)
├── main.py      # Punto de entrada
├── requirements.txt
└── ejemplo_datos_charpy.txt   # Archivo de ejemplo para probar la app
```

## Instalación

```
pip install -r requirements.txt
```

Si en Linux aparece un error de que falta `tkinter`, es un paquete del
sistema operativo, no de pip:

```
sudo apt install python3-tk        # Ubuntu/Debian
sudo dnf install python3-tkinter   # Fedora
```

(En Windows y macOS ya viene incluido con el instalador oficial de Python).

## Uso

```
python main.py
```

1. **Cargar archivo .txt**: seleccioná el archivo de datos. Podés generarlo
   copiando la salida del comando `d` del Arduino (tabla Tiempo_us /
   Angulo_grados). Si además pegás el bloque `===== RESULTADO =====`
   (comando `e`) en el mismo archivo, la app lo aprovecha automáticamente
   para pre-completar la fricción y usar el ángulo de rebote exacto en
   vez de estimarlo.
2. Completá masa, radio y, si querés energía cinética/potencia, el momento
   de inercia (manual o calculado a partir del período de oscilación).
3. **Calcular** y mirá las pestañas: Ángulo, Velocidad, Aceleración,
   Energía y potencia.
4. **Exportar resultados...**: elegí PDF (reporte con parámetros,
   resultados y los 4 gráficos), JPG (cada gráfico por separado) o TXT
   (tabla de datos procesados: ángulo suavizado, velocidad, aceleración
   y energías, separada por tabuladores).

Probá primero con `ejemplo_datos_charpy.txt` incluido en la carpeta.

## Suavizado opcional

`CubicSpline` interpola exactamente por todos los puntos crudos, ruido de
cuantización del encoder incluido. Eso no afecta demasiado al ángulo, pero
al derivar dos veces para aceleración —y sobre todo para potencia— ese
ruido queda amplificado y las curvas pueden salir con dientes de sierra.
Si te pasa, cargá un valor impar (9 u 11) en "Ventana Savitzky-Golay" antes
de Calcular: pre-suaviza el ángulo crudo antes de construir el spline. Por
defecto queda vacío (sin pre-suavizado), tal como se especificó original-
mente.

## Nota sobre el firmware

El Arduino solo guarda 100 muestras a 1 ms (100 ms máximo) por limitación
de RAM del Uno. Si el ensayo real tarda más en llegar al pico de rebote,
la tabla queda truncada y la app te avisa cuando el valor de energía es
una estimación (probablemente sobrestimada) en vez del resultado exacto.
Para evitarlo, incluí siempre el bloque `RESULTADO` (comando `e`) en el
archivo que cargues.
