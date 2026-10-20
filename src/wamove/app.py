from __future__ import annotations

from dataclasses import dataclass

from .ios.backup import WA_BUSINESS_DOMAIN, WA_DOMAIN


@dataclass(frozen=True)
class App:
    key: str
    title: str
    package: str
    folder: str
    ios_domain: str

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(f"{base}/Android/media/{self.package}/{self.folder}"
                     for base in ("/sdcard", "/storage/emulated/0"))

    def media_dirs(self) -> tuple[str, ...]:
        return tuple(f"{self.folder} {kind}" for kind in MEDIA_KINDS)


MEDIA_KINDS = ("Images", "Video", "Voice Notes", "Audio", "Documents", "Animated Gifs", "Stickers", "Video Notes")

WHATSAPP = App("whatsapp", "WhatsApp", "com.whatsapp", "WhatsApp", WA_DOMAIN)
# The Business iOS domain is unverified on a real backup; `wamove inspect` shows whether the backup holds it.
BUSINESS = App("business", "WhatsApp Business", "com.whatsapp.w4b", "WhatsApp Business", WA_BUSINESS_DOMAIN)
APPS = {app.key: app for app in (WHATSAPP, BUSINESS)}
