"""Gera o .ico usado pelo executavel e pelo instalador."""
import sys

from deck import tray_image


if __name__ == "__main__":
    tray_image(256).save(
        sys.argv[1],
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
