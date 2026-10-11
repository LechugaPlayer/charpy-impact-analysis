"""
main.py
=======
Punto de entrada de la aplicación de análisis de ensayo Charpy.
Verifica que las dependencias estén instaladas (con un mensaje claro si
falta alguna) y luego lanza la interfaz gráfica definida en gui.py.

Uso:
    python main.py
"""

import sys


def _verificar_dependencias() -> None:
    """Chequea las librerías necesarias y corta con un mensaje claro (en vez
    de un traceback críptico) si falta alguna."""
    faltantes_pip = []

    for modulo, paquete_pip in [
        ("customtkinter", "customtkinter"),
        ("matplotlib", "matplotlib"),
        ("numpy", "numpy"),
        ("scipy", "scipy"),
    ]:
        try:
            __import__(modulo)
        except ImportError:
            faltantes_pip.append(paquete_pip)

    tkinter_disponible = True
    try:
        __import__("tkinter")
    except ImportError:
        tkinter_disponible = False

    if faltantes_pip or not tkinter_disponible:
        print("No se pudo iniciar el Analizador de Ensayo Charpy: faltan dependencias.\n")

        if faltantes_pip:
            print("Instalá con pip:")
            print(f"  pip install {' '.join(faltantes_pip)}")
            print("  (o simplemente: pip install -r requirements.txt)\n")

        if not tkinter_disponible:
            print("Falta 'tkinter' (viene con Python, pero algunas distros Linux lo")
            print("separan en un paquete del sistema, no de pip):")
            print("  Ubuntu/Debian:  sudo apt install python3-tk")
            print("  Fedora:         sudo dnf install python3-tkinter")
            print("  Windows/macOS:  ya viene incluido con el instalador oficial de Python.\n")

        sys.exit(1)


def main() -> None:
    _verificar_dependencias()

    # Import diferido: recien se importa gui.py (y con el, customtkinter)
    # despues de confirmar que las dependencias existen, para no mostrar
    # un traceback si falta algo.
    from gui import CharpyApp

    app = CharpyApp()
    app.mainloop()


if __name__ == "__main__":
    main()
