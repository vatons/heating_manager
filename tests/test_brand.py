"""The integration ships its own brand images, which Home Assistant serves (and HACS checks for)."""
from pathlib import Path

from PIL import Image

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.heating_manager.const import DOMAIN

BRAND = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN / "brand"


async def test_home_assistant_finds_the_brand_images(hass: HomeAssistant):
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.has_branding


def test_icons_follow_the_brand_guidelines():
    """Square PNGs with transparency: 256 px, and 512 px for @2x."""
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        with Image.open(BRAND / name) as image:
            assert image.format == "PNG"
            assert image.size == (size, size)
            assert image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0  # transparent background
