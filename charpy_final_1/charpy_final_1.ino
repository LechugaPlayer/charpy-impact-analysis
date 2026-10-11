// ============================================================
// Maquina de Charpy casera - Medicion de energia absorbida
// Encoder: E38S6G5-600B-G24N (600 PPR, cuadratura A/B)
// Arduino Uno (ATmega328P)
//
// VERSION FINAL - SIN OLED
//   El OLED SSD1306 y sus dependencias (Adafruit_GFX, Adafruit_SSD1306,
//   Wire) fueron removidos por completo. Esa decision libera >1KB de
//   SRAM (el framebuffer del SSD1306 solo ya ocupaba 1024 bytes), que
//   se reinvirtio en subir el buffer de captura de 100 a 300 muestras.
//   Toda la salida "en vivo" que antes iba al OLED ahora vive en el
//   Monitor Serie (banner, barra de angulo, bloque de resultado).
//
// CORRECCION DE LA MAQUINA DE ESTADOS DE CAPTURA (esta version)
//   Se detectaron y corrigieron 3 problemas en la logica que decide
//   cuando "empezo" y "termino" el ensayo tras presionar 's':
//
//   1) FALSO DISPARO MIENTRAS SE SOSTIENE EL PENDULO. El detector de
//      quietud arrancaba su cronometro en el momento de presionar 's',
//      no en el momento real de soltar. Si el usuario tardaba mas de
//      TIEMPO_QUIETO_MS (800ms, viejo) en soltar, la mano quieta
//      sosteniendo el pendulo se interpretaba como "ya freno" y se
//      imprimia el resultado ANTES de soltar. Fix: nuevo estado
//      ESPERANDO_LIBERACION que no arranca ningun cronometro; solo
//      avanza cuando el angulo se aleja mas de UMBRAL_LIBERACION_GRADOS
//      del angulo inicial.
//
//   2) EL ANGULO DE REBOTE NUNCA SE ACTUALIZABA. anguloMaximoSubida se
//      inicializaba en el angulo de liberacion (el mayor valor posible
//      en magnitud de todo el ensayo), y como el rebote SIEMPRE es menor
//      en magnitud que la liberacion, la condicion
//      abs(anguloActual) > abs(anguloMaximoSubida) nunca se volvia a
//      cumplir. Resultado: el "angulo final (rebote)" reportado quedaba
//      pegado al angulo inicial y la energia absorbida daba ~0 J
//      siempre, sin importar el ensayo real. Fix: anguloMaximoSubida se
//      reinicia en el momento del cruce por el cero mecanico (fin de la
//      caida, inicio de la subida del rebote), no antes.
//
//   3) ESPERAR A QUE EL PENDULO SE DETENGA DEL TODO ES INNECESARIO Y
//      LENTO. Con rodamientos de baja friccion el pendulo puede seguir
//      oscilando (en decaimiento) varios segundos despues del primer
//      pico de rebote, que es el unico dato que importa fisicamente.
//      Fix: en vez de esperar "quietud sostenida", se detecta el PICO
//      real (el angulo empieza a bajar de forma clara, por encima del
//      ruido de cuantizacion del encoder de 0.15 grados/cuenta) y se
//      corta la captura ahi mismo, sin esperar el resto del decaimiento.
//      Ademas, el inicio de la ventana de muestreo (numMuestras,
//      tiempoInicioCapturaUs) se movio del momento de presionar 's' al
//      momento real de la suelta, para no gastar buffer en muestras
//      "planas" mientras la mano todavia sostiene el pendulo.
//
// ------------------------------------------------------------
// CONEXION ENCODER (unico periferico externo del sistema):
//   VCC -> 5V del Uno, o fuente externa 9-12V si el encoder no
//          responde con 5V directo (ver nota de proyecto sobre el
//          dropout del regulador interno del E38S6G5).
//   GND -> GND (comun con el Arduino)
//   A   -> Pin 2 (INT0)
//   B   -> Pin 3 (INT1)
//   Shield/drenaje (si esta presente) -> GND
// ------------------------------------------------------------
//
// FORMATO DE SALIDA POR SERIAL:
//   Todo el texto "decorativo" (banner, menu, barra de angulo en vivo)
//   fue disenado para que NO matchee los patrones regex de engine.py:
//   arranca siempre con letras, nunca con un digito al principio de
//   linea, asi el parser lo descarta como ruido de forma segura.
//   El bloque RESULTADO usa un borde con prefijo "| " pero conserva
//   intacto el texto clave ("Angulo inicial:", "Angulo final (rebote):",
//   etc.) que buscan los patrones de _PATRONES_METADATA en engine.py
//   (los busca con re.search, no con match anclado, asi que el borde
//   alrededor no rompe nada). La tabla de datos (comando 'd') se dejo
//   SIN bordes a proposito: esa si usa un patron anclado con ^...$,
//   y agregarle caracteres de borde la habria roto.
// ============================================================

#include <avr/pgmspace.h>  // PROGMEM y pgm_read_byte() para tablaTransicion

// --- Pines del encoder (usamos puerto D para acceso directo) ---
// Pin 2 = PD2, Pin 3 = PD3
const byte PIN_A = 2;   // Debe ir en pin de interrupcion (2 o 3 en Uno)
const byte PIN_B = 3;   // Debe ir en pin de interrupcion (2 o 3 en Uno)

// --- Constantes del encoder ---
const int PPR = 600;                          // Pulsos por revolucion (1 canal)
const float CONTEOS_POR_VUELTA = PPR * 4.0;   // x4 porque contamos ambos flancos de A y B
const float GRADOS_POR_CONTEO = 360.0 / CONTEOS_POR_VUELTA;  // = 0.15 grados/cuenta

// --- Constantes mecanicas de TU pendulo (AJUSTAR ESTOS DOS VALORES) ---
float masa_pendulo = 1.68;        // kg  <-- CAMBIAR por tu valor real
float radio_centro_masa = 0.38;  // m   <-- distancia eje-centro de masa, CAMBIAR
const float g = 9.81;            // m/s^2

// --- Variables del encoder (optimizadas para velocidad) ---
volatile int32_t conteo = 0;          // contador de pulsos (4x)
volatile uint8_t estadoAnteriorAB = 0; // estado previo de A y B (bits: A=bit1, B=bit0)

// --- Tabla de transiciones para decodificacion x4 (mas rapida que switch) ---
// Indice = (estadoAnterior << 2) | estadoActual
// Devuelve +1, -1 o 0 segun la direccion
// PROGMEM: tabla de solo lectura que NO cambia en tiempo de ejecucion, asi
// que se deja en flash en vez de copiarla a RAM (ahorra 16 bytes de SRAM).
static const int8_t tablaTransicion[16] PROGMEM = {
  0,  // 0000 (0)
  1,  // 0001 (1)
  -1, // 0010 (2)
  0,  // 0011 (3)
  -1, // 0100 (4)
  0,  // 0101 (5)
  0,  // 0110 (6)
  1,  // 0111 (7)
  1,  // 1000 (8)
  0,  // 1001 (9)
  0,  // 1010 (10)
  -1, // 1011 (11)
  0,  // 1100 (12)
  -1, // 1101 (13)
  1,  // 1110 (14)
  0   // 1111 (15)
};

// --- Variables del experimento ---
float anguloInicial = 0;
bool anguloInicialGuardado = false;

float anguloMaximoSubida = 0;   // durante EN_SUBIDA_REBOTE: pico real del rebote
bool capturandoSubida = false;  // true desde 's' hasta que se confirma el pico
bool golpeDetectado = false;

float energiaBrutaResultado = 0;
float energiaNetaResultado = 0;
bool hayResultado = false;

// --- Maquina de estados del ensayo (reemplaza el viejo detector de
//     "quietud sostenida", ver nota de correccion al inicio del archivo) ---
enum EstadoEnsayo : uint8_t {
  ESPERANDO_LIBERACION,   // 's' presionado, esperando que se suelte el pendulo
  EN_CAIDA_HACIA_IMPACTO, // se solto, cayendo hacia el punto mas bajo (probeta)
  EN_SUBIDA_REBOTE        // ya cruzo el punto mas bajo, subiendo hacia el pico de rebote
};
EstadoEnsayo estadoEnsayo = ESPERANDO_LIBERACION;
unsigned long tiempoInicioEstadoMs = 0;  // marca de tiempo de la ultima transicion de estado

const float UMBRAL_LIBERACION_GRADOS = 3.0;  // separacion minima del angulo inicial para confirmar la suelta
const float UMBRAL_REVERSION_GRADOS = 1.0;   // caida minima desde el maximo para confirmar el pico de rebote (>> ruido de 0.15 grados/cuenta del encoder)
const unsigned long TIMEOUT_CAIDA_MS = 3000UL;         // margen amplio sobre los ~484ms calculados (integral eliptica, liberacion a 150 grados)
const unsigned long TIMEOUT_SUBIDA_REBOTE_MS = 5000UL; // si no se confirma una reversion clara en este tiempo, se corta igual con aviso

// --- Calibracion de perdidas por friccion (tirada en vacio, sin probeta) ---
float energiaPerdidaFriccion = 0.0;  // Joules, se mide con el comando 'c'
bool friccionCalibrada = false;

// --- Registro de datos para grafico ---
// MAX_MUESTRAS = 300 a INTERVALO_MUESTREO_US = 3500 (3.5 ms) => ventana de
// captura de ~1050 ms. Ese valor de intervalo no es arbitrario: se calculo
// resolviendo la integral eliptica del pendulo de gran amplitud para un
// angulo de liberacion de 150 grados, que da ~484 ms hasta el impacto con
// la probeta; 1050 ms deja margen de sobra para capturar tambien el rebote
// posterior sin depender de la metadata del bloque RESULTADO como respaldo.
// Esta ventana ahora arranca en la SUELTA real (ver EN_CAIDA_HACIA_IMPACTO
// mas abajo), no en el momento de presionar 's', asi que no se desperdicia
// buffer en muestras "planas" mientras la mano todavia sostiene el pendulo.
// RAM: cada muestra ocupa 4 bytes (2 para delta de tiempo + 2 para angulo
// x100), asi que 300 muestras = 1200 bytes. Ese presupuesto es el motivo
// central por el que hubo que sacar el OLED (framebuffer de 1024 bytes),
// que sumado a Adafruit_GFX y Wire dejaba apenas margen para 100 muestras.
const int MAX_MUESTRAS = 300;
uint16_t registroDeltaTiempoUs[MAX_MUESTRAS]; // delta desde la muestra anterior, en us
int16_t registroAnguloX100[MAX_MUESTRAS];      // angulo * 100 (ej: 45.07 grados -> 4507)
int numMuestras = 0;
uint32_t tiempoInicioCapturaUs = 0;
uint32_t ultimoMuestreoUs = 0;
uint32_t tiempoUltimaMuestraAbsUs = 0; // absoluto, solo para calcular el proximo delta
const uint32_t INTERVALO_MUESTREO_US = 3500; // 3.5 ms entre muestras

void setup() {
  Serial.begin(115200);

  // Configurar pines como entrada con pull-up (colector abierto NPN del encoder)
  pinMode(PIN_A, INPUT_PULLUP);
  pinMode(PIN_B, INPUT_PULLUP);

  // Leer estado inicial de A y B (bits: A=bit1, B=bit0)
  uint8_t portD = PIND;
  estadoAnteriorAB = ((portD & (1 << PIN_A)) ? 0x02 : 0) | ((portD & (1 << PIN_B)) ? 0x01 : 0);

  // Adjuntar interrupciones en ambos flancos
  attachInterrupt(digitalPinToInterrupt(PIN_A), leerEncoder, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_B), leerEncoder, CHANGE);

  imprimirBanner();

  Serial.println(F("Comandos disponibles:"));
  Serial.println(F("  z -> Pone el angulo actual como CERO (punto mas bajo, vertical)"));
  Serial.println(F("  i -> Marca el angulo actual como angulo INICIAL (antes de soltar el pendulo)"));
  Serial.println(F("  s -> Inicia captura del angulo maximo tras el golpe"));
  Serial.println(F("  e -> Calcula y muestra la ENERGIA ABSORBIDA (Joules)"));
  Serial.println(F("  c -> Calibra perdida por friccion (tirada en vacio, SIN probeta)"));
  Serial.println(F("  d -> Imprime la tabla de datos (tiempo_us, angulo) del ultimo ensayo"));
  Serial.println(F("  m -> Muestra la memoria RAM libre estimada (diagnostico)"));
  Serial.println();
}

void loop() {
  // --- Procesar comandos por Serial ---
  if (Serial.available()) {
    char c = Serial.read();

    if (c == 'z') {
      noInterrupts();
      conteo = 0;
      interrupts();
      Serial.println(F(">> Cero establecido en posicion actual."));
    }
    else if (c == 'i') {
      anguloInicial = obtenerAnguloGrados();
      anguloInicialGuardado = true;
      hayResultado = false;
      Serial.print(F(">> Angulo inicial guardado: "));
      Serial.print(anguloInicial);
      Serial.println(F(" grados"));
      if (abs(anguloInicial) < UMBRAL_LIBERACION_GRADOS) {
        Serial.println(F("!! Aviso: el angulo inicial esta muy cerca de cero. La deteccion de"));
        Serial.println(F("   suelta y de cruce por cero puede fallar. Revisa el comando 'z'."));
      }
    }
    else if (c == 's') {
      if (!anguloInicialGuardado) {
        Serial.println(F("!! Aviso: no has guardado el angulo inicial con 'i'. Hazlo antes de soltar el pendulo."));
      }
      capturandoSubida = true;
      golpeDetectado = false;
      hayResultado = false;
      estadoEnsayo = ESPERANDO_LIBERACION;
      anguloMaximoSubida = obtenerAnguloGrados(); // valor provisional; se reinicia al cruzar por cero
      tiempoInicioEstadoMs = millis();

      // numMuestras y tiempoInicioCapturaUs se reinician recien cuando se
      // detecta la suelta real (transicion a EN_CAIDA_HACIA_IMPACTO), no
      // aca: ver nota de correccion #3 al inicio del archivo.

      Serial.println(F(">> Listo. Suelta el pendulo cuando quieras (no hace falta apurarse)."));
    }
    else if (c == 'e') {
      if (capturandoSubida && !golpeDetectado) {
        Serial.println(F("!! Aviso: el ensayo todavia esta en curso (esperando el pico de rebote)."));
        Serial.println(F("   Espera a que se imprima el resultado automaticamente."));
      } else {
        calcularYMostrarEnergia();
      }
    }
    else if (c == 'c') {
      if (!anguloInicialGuardado || !golpeDetectado) {
        Serial.println(F("!! Para calibrar friccion: primero 'i', luego 's', suelta el pendulo SIN probeta, espera a que se detenga, y vuelve a presionar 'c'."));
      } else {
        float thetaInicialRad = radians(abs(anguloInicial));
        float thetaFinalRad = radians(abs(anguloMaximoSubida));
        energiaPerdidaFriccion = masa_pendulo * g * radio_centro_masa *
                                   (cos(thetaFinalRad) - cos(thetaInicialRad));
        friccionCalibrada = true;
        Serial.print(F(">> Perdida por friccion calibrada: "));
        Serial.print(energiaPerdidaFriccion, 4);
        Serial.println(F(" J (se restara automaticamente en futuros calculos con 'e')"));
      }
    }
    else if (c == 'd') {
      imprimirTablaDatos();
    }
    else if (c == 'm') {
      Serial.print(F(">> Memoria libre estimada (heap/stack): "));
      Serial.print(memoriaLibre());
      Serial.println(F(" bytes"));
    }
  }

  // --- Captura de datos durante el ensayo (ALTA PRIORIDAD) ---
  if (capturandoSubida && !golpeDetectado) {
    float anguloActual = obtenerAnguloGrados();
    uint32_t ahoraUs = micros();

    switch (estadoEnsayo) {

      // --------------------------------------------------------
      // 1) Esperando que el usuario suelte el pendulo. NO se arranca
      //    ningun cronometro de "quietud" aca: una mano quieta
      //    sosteniendo el pendulo en el angulo inicial se veria
      //    identica a "ya freno" (ese era el bug original). Solo se
      //    avanza de estado cuando el angulo se aleja lo suficiente
      //    del angulo inicial guardado con 'i'.
      // --------------------------------------------------------
      case ESPERANDO_LIBERACION:
        if (abs(anguloActual - anguloInicial) > UMBRAL_LIBERACION_GRADOS) {
          estadoEnsayo = EN_CAIDA_HACIA_IMPACTO;
          tiempoInicioEstadoMs = millis();

          // La ventana de MAX_MUESTRAS arranca aca (suelta real), no en
          // el momento de presionar 's'.
          numMuestras = 0;
          tiempoInicioCapturaUs = ahoraUs;
          ultimoMuestreoUs = 0;
          tiempoUltimaMuestraAbsUs = ahoraUs;

          Serial.println(F(">> Suelta detectada. Cayendo hacia el impacto..."));
        }
        break;

      // --------------------------------------------------------
      // 2) Cayendo desde el angulo inicial hacia el punto mas bajo
      //    (donde esta la probeta). El impacto se detecta cuando el
      //    angulo cruza el cero mecanico (cambia de signo respecto al
      //    angulo inicial); a partir de ahi arranca la subida real
      //    del rebote.
      // --------------------------------------------------------
      case EN_CAIDA_HACIA_IMPACTO:
        registrarMuestra(anguloActual, ahoraUs);

        if ((anguloInicial > 0 && anguloActual <= 0) ||
            (anguloInicial < 0 && anguloActual >= 0)) {
          estadoEnsayo = EN_SUBIDA_REBOTE;
          tiempoInicioEstadoMs = millis();

          // Reinicio CLAVE (fix del bug #2): el maximo de subida arranca
          // desde el cruce por cero, no desde el angulo inicial. Antes de
          // este fix, abs(anguloActual) > abs(anguloMaximoSubida) nunca
          // se volvia a cumplir porque el rebote siempre es menor en
          // magnitud que la liberacion, y el "angulo final (rebote)"
          // quedaba pegado al angulo inicial (energia ~0 J siempre).
          anguloMaximoSubida = anguloActual;
        } else if (millis() - tiempoInicioEstadoMs > TIMEOUT_CAIDA_MS) {
          // Salvaguarda: el cruce deberia ocurrir en unos cientos de ms
          // (ver calculo de integral eliptica de INTERVALO_MUESTREO_US).
          // Si no ocurre, algo anda mal (encoder, angulo inicial, etc.):
          // es mas seguro abortar que quedar bloqueado indefinidamente.
          Serial.println(F("!! Aviso: no se detecto el cruce por el punto mas bajo a tiempo."));
          Serial.println(F("   Verifica el encoder y el angulo inicial ('i'), y reintenta con 's'."));
          capturandoSubida = false;
          estadoEnsayo = ESPERANDO_LIBERACION;
        }
        break;

      // --------------------------------------------------------
      // 3) Subiendo tras el impacto. Se trackea el maximo en magnitud
      //    (pico del rebote). NO se espera a que el pendulo se detenga
      //    del todo -- con rodamientos de baja friccion eso puede
      //    tardar varios segundos de oscilaciones decrecientes -- sino
      //    que se confirma el pico apenas el angulo empieza a bajar de
      //    forma clara (por encima del ruido de cuantizacion del
      //    encoder, 0.15 grados/cuenta).
      // --------------------------------------------------------
      case EN_SUBIDA_REBOTE:
        registrarMuestra(anguloActual, ahoraUs);

        if (abs(anguloActual) > abs(anguloMaximoSubida)) {
          anguloMaximoSubida = anguloActual;
        } else if (abs(anguloMaximoSubida) - abs(anguloActual) > UMBRAL_REVERSION_GRADOS) {
          golpeDetectado = true;
          capturandoSubida = false;
          estadoEnsayo = ESPERANDO_LIBERACION;
          Serial.print(F(">> Pico de rebote detectado. Angulo maximo de rebote: "));
          Serial.print(anguloMaximoSubida);
          Serial.println(F(" grados"));
          Serial.println(F(">> Escribe 'e' para calcular la energia absorbida, o 'd' para ver la tabla de datos."));
          calcularYMostrarEnergia();
        } else if (millis() - tiempoInicioEstadoMs > TIMEOUT_SUBIDA_REBOTE_MS) {
          // Salvaguarda equivalente: si nunca se confirma una reversion
          // clara (encoder ruidoso, friccion inusualmente alta), se
          // corta igual con el mejor valor disponible, avisando que no
          // fue una deteccion limpia.
          golpeDetectado = true;
          capturandoSubida = false;
          estadoEnsayo = ESPERANDO_LIBERACION;
          Serial.println(F("!! Aviso: no se confirmo una reversion clara del angulo (timeout)."));
          Serial.print(F(">> Se usa como angulo de rebote el maximo registrado: "));
          Serial.print(anguloMaximoSubida);
          Serial.println(F(" grados"));
          calcularYMostrarEnergia();
        }
        break;
    }
  }

  // --- Barra de angulo en vivo por Serial (reemplaza la pantalla OLED) ---
  // Solo se imprime si NO estamos capturando y todavia NO hay un resultado
  // calculado. Igual que en la version con OLED, una vez que hayResultado
  // es true el Monitor Serie queda en silencio (no repite el bloque cada
  // 100ms) hasta el proximo 'i' o 's': eso es lo que evita que el bloque
  // RESULTADO se empuje fuera de pantalla en pocos segundos.
  static unsigned long ultimoReporte = 0;
  if (!capturandoSubida && !hayResultado && (millis() - ultimoReporte > 100)) {
    ultimoReporte = millis();
    imprimirBarraAngulo(obtenerAnguloGrados());
  }
}

// ============================================
// INTERRUPCION DEL ENCODER (OPTIMIZADA - LECTURA DIRECTA DE PUERTO)
// ============================================
void leerEncoder() {
  uint8_t portD = PIND;
  uint8_t estadoActual = ((portD & (1 << PIN_A)) ? 0x02 : 0) | ((portD & (1 << PIN_B)) ? 0x01 : 0);
  uint8_t indice = (estadoAnteriorAB << 2) | estadoActual;
  int8_t delta = (int8_t)pgm_read_byte(&tablaTransicion[indice]); // leer desde flash, no desde RAM
  conteo += delta;
  estadoAnteriorAB = estadoActual;
}

// ============================================
// LECTURA SEGURA DEL ANGULO
// ============================================
float obtenerAnguloGrados() {
  int32_t conteoLocal;
  noInterrupts();
  conteoLocal = conteo;
  interrupts();
  return conteoLocal * GRADOS_POR_CONTEO;
}

// ============================================
// REGISTRO DE MUESTRAS (tabla tiempo/angulo para el comando 'd')
// ============================================
// Extraido a funcion propia porque ahora se llama desde DOS estados
// distintos de la maquina de estados (EN_CAIDA_HACIA_IMPACTO y
// EN_SUBIDA_REBOTE); antes de esta correccion estaba inline en un solo
// lugar. Throttlea internamente a INTERVALO_MUESTREO_US y respeta el
// limite de MAX_MUESTRAS (deja de grabar, pero no bloquea el resto del
// ensayo: el trackeo de anguloMaximoSubida sigue funcionando igual con
// el buffer lleno).
void registrarMuestra(float anguloActual, uint32_t ahoraUs) {
  if (ahoraUs - ultimoMuestreoUs >= INTERVALO_MUESTREO_US && numMuestras < MAX_MUESTRAS) {
    uint32_t deltaUs = ahoraUs - tiempoUltimaMuestraAbsUs;
    if (deltaUs > 65535UL) deltaUs = 65535UL; // salvaguarda defensiva, no deberia ocurrir en uso normal

    registroDeltaTiempoUs[numMuestras] = (uint16_t)deltaUs;
    registroAnguloX100[numMuestras] = (int16_t)(anguloActual * 100.0);
    numMuestras++;

    ultimoMuestreoUs = ahoraUs;
    tiempoUltimaMuestraAbsUs = ahoraUs;
  }
}

// ============================================
// CALCULO DE ENERGIA (con verificacion de positividad)
// ============================================
void calcularYMostrarEnergia() {
  if (!anguloInicialGuardado) {
    Serial.println(F("!! No se ha guardado el angulo inicial (usa 'i' primero)."));
    return;
  }

  float thetaInicialRad = radians(abs(anguloInicial));
  float thetaFinalRad = radians(abs(anguloMaximoSubida));

  energiaBrutaResultado = masa_pendulo * g * radio_centro_masa *
                            (cos(thetaFinalRad) - cos(thetaInicialRad));

  energiaNetaResultado = energiaBrutaResultado - energiaPerdidaFriccion;

  // Verificar que la energia no sea negativa
  if (energiaNetaResultado < 0) {
    Serial.println(F("!! ADVERTENCIA: La energia calculada es negativa."));
    Serial.println(F("   Posibles causas: angulo final > inicial, error de calibracion,"));
    Serial.println(F("   o el pendulo no impacto la probeta correctamente."));
    Serial.println(F("   Se mostrara 0.0000 J como valor fisico."));
    energiaNetaResultado = 0.0;
  }

  hayResultado = true;

  imprimirBloqueResultado();
}

// ============================================
// BLOQUE RESULTADO CON BORDES (reemplaza la pantalla OLED de resultado)
// ============================================
// Usa dtostrf() para formatear cada numero con ancho fijo: eso evita que
// la alineacion "baile" segun la cantidad de digitos o el signo del valor,
// y coincide con el mismo criterio de alineacion usado en la barra de
// angulo en vivo. El borde ("+===...+", "| ...") es puramente cosmetico:
// no se cierra con "|" a la derecha porque calcular el padding exacto por
// linea agregaria complejidad y uso de RAM (strings intermedios) para un
// beneficio solo estetico en un Monitor Serie de texto plano.
//
// IMPORTANTE: el texto clave de cada linea (p. ej. "Angulo inicial:",
// "Energia absorbida NETA (probeta):") se mantiene IDENTICO al de la
// version anterior a proposito, porque _PATRONES_METADATA en engine.py
// lo busca con re.search() dentro de la linea completa. El prefijo "| "
// no rompe esa busqueda; se valido explicitamente antes de implementar
// (ver bloque de pruebas que acompana esta entrega).
void imprimirBloqueResultado() {
  char buf[12];

  Serial.println();
  Serial.println(F("+==========================================+"));
  Serial.println(F("|           RESULTADO DEL ENSAYO            |"));
  Serial.println(F("+==========================================+"));

  dtostrf(anguloInicial, 8, 2, buf);
  Serial.print(F("| Angulo inicial: "));
  Serial.print(buf);
  Serial.println(F(" grados"));

  dtostrf(anguloMaximoSubida, 8, 2, buf);
  Serial.print(F("| Angulo final (rebote): "));
  Serial.print(buf);
  Serial.println(F(" grados"));

  dtostrf(energiaBrutaResultado, 9, 4, buf);
  Serial.print(F("| Energia bruta (sin corregir): "));
  Serial.print(buf);
  Serial.println(F(" J"));

  if (friccionCalibrada) {
    dtostrf(energiaPerdidaFriccion, 9, 4, buf);
    Serial.print(F("| Perdida por friccion (calibrada): "));
    Serial.print(buf);
    Serial.println(F(" J"));

    dtostrf(energiaNetaResultado, 9, 4, buf);
    Serial.print(F("| Energia absorbida NETA (probeta): "));
    Serial.print(buf);
    Serial.println(F(" J"));
  } else {
    Serial.println(F("| (Aun no calibras friccion con 'c'. Este valor incluye"));
    Serial.println(F("| perdidas por rodamientos/aire.)"));
  }

  Serial.println(F("+==========================================+"));
  Serial.println();
}

// ============================================
// BARRA DE ANGULO EN VIVO (reemplaza la pantalla OLED de angulo en vivo)
// ============================================
// Imprime el angulo actual con ancho fijo (dtostrf: 7 caracteres, 2
// decimales) seguido de una barra ASCII de 20 caracteres que ubica ese
// angulo dentro del rango -180..+180 grados. El '|' central marca el
// cero mecanico (posicion vertical fijada con el comando 'z').
void imprimirBarraAngulo(float angulo) {
  const int ANCHO_BARRA = 20;
  const float RANGO_MIN = -180.0;
  const float RANGO_MAX = 180.0;

  float angRecortado = angulo;
  if (angRecortado < RANGO_MIN) angRecortado = RANGO_MIN;
  if (angRecortado > RANGO_MAX) angRecortado = RANGO_MAX;

  int posMarcador = (int)(((angRecortado - RANGO_MIN) / (RANGO_MAX - RANGO_MIN)) * (ANCHO_BARRA - 1) + 0.5);

  char bufAngulo[10];
  dtostrf(angulo, 7, 2, bufAngulo);

  Serial.print(F("Angulo: "));
  Serial.print(bufAngulo);
  Serial.print(F(" deg  ["));
  for (int i = 0; i < ANCHO_BARRA; i++) {
    if (i == posMarcador) {
      Serial.print('#');
    } else if (i == ANCHO_BARRA / 2) {
      Serial.print('|');
    } else {
      Serial.print('-');
    }
  }
  Serial.println(F("]"));
}

// ============================================
// BANNER DE INICIO (reemplaza la pantalla de bienvenida OLED)
// ============================================
void imprimirBanner() {
  Serial.println();
  Serial.println(F("+================================================+"));
  Serial.println(F("|        MAQUINA DE ENSAYO CHARPY CASERA          |"));
  Serial.println(F("|      Encoder E38S6G5-600B-G24N - 600 PPR x4     |"));
  Serial.println(F("+================================================+"));
  Serial.println();
}

// ============================================
// IMPRIMIR TABLA DE DATOS
// ============================================
// SIN bordes a proposito: read_arduino_data() en engine.py matchea cada
// fila con un patron anclado (^...$) que espera <entero><separador><float>
// y nada mas en la linea. Agregar un prefijo de borde aca rompe ese match.
void imprimirTablaDatos() {
  if (numMuestras == 0) {
    Serial.println(F("!! No hay datos registrados todavia. Haz un ensayo con 's' primero."));
    return;
  }

  Serial.println(F("===== TABLA DE DATOS (copiar y pegar en Excel/Sheets) ====="));
  Serial.println(F("Tiempo_us\tAngulo_grados"));

  // Reconstruye el tiempo ABSOLUTO sumando los deltas guardados. El valor
  // impreso en cada fila es identico al formato que espera engine.py.
  uint32_t tiempoAcumuladoUs = 0;
  for (int i = 0; i < numMuestras; i++) {
    tiempoAcumuladoUs += registroDeltaTiempoUs[i];
    Serial.print(tiempoAcumuladoUs);
    Serial.print(F("\t"));
    Serial.println(registroAnguloX100[i] / 100.0, 2);
  }
  Serial.println(F("====== FIN DE TABLA ======"));
  Serial.print(F("Total de puntos: "));
  Serial.println(numMuestras);
}

// ============================================
// DIAGNOSTICO DE MEMORIA (uso: comando 'm' por Serial)
// ============================================
// Estima la RAM libre entre el heap y el puntero de stack actual (SP).
// Util para confirmar en la practica, con el pendulo funcionando de
// verdad, que el margen se sostiene -- sobre todo justo despues de un
// calculo de energia con cos()/radians(), el peor caso de profundidad
// de pila de todo el sketch.
int memoriaLibre() {
  extern int __heap_start, *__brkval;
  int valorLocal;
  return (int)&valorLocal - (__brkval == 0 ? (int)&__heap_start : (int)__brkval);
}
