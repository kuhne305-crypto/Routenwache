import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, time as dt_time, timedelta
import pytz
import json
import os
import re

# ════════════════════════════════════════════════════════════════════════════
# ⚙️  KONFIGURATION
# ════════════════════════════════════════════════════════════════════════════
TOKEN    = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID") or "1526202327365582910"
TIMEZONE = pytz.timezone("Europe/Berlin")
EMBED_COLOR = 0xFFD700  # Gelb
DATA_DIR  = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Wie viele Zeiträume darf EIN Mitglied an EINEM Tag auf DERSELBEN Route
# gleichzeitig eingetragen sein (z.B. zwei aufeinanderfolgende Schichten).
MAX_ZEITRAEUME_PRO_USER = 2

# "Morteco-Vorlage": Diese Zeiträume bekommt JEDE neu erstellte Route
# automatisch (kann bei /route_erstellen mit standard_zeitraeume:False
# abgeschaltet werden, dann startet die Route ohne Zeiträume).
STANDARD_SLOTS = ["18:00-19:30", "19:30-21:00", "21:00-22:30", "22:30-00:00"]

# Leitung: darf Routen verwalten, Channels setzen, Routensperren verhängen.
# Ein-/Austragen in einen Zeitraum ist bewusst für ALLE offen.
LEITUNG_ROLLE_ID = 1526202327483285629


def ist_admin_oder_leitung(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == LEITUNG_ROLLE_ID for r in interaction.user.roles)


# ════════════════════════════════════════════════════════════════════════════
# 💾  DATENSPEICHER
# ════════════════════════════════════════════════════════════════════════════
# Struktur:
# "routen": {
#     "morteco_heroin_route": {
#         "name": "Morteco Heroin Route",
#         "kapazitaet": 2,
#         "slots": ["18:00-19:30", "19:30-21:00"],   # frei konfigurierbar, beliebig viele
#         "channel_buttons": 123, "stempel_nachricht_id": "456",
#         "channel_log": 123,
#         "channel_leaderboard": 123, "leaderboard_nachricht_id": "456",
#         "ping_rolle_id": None,   # optional: wird beim Tageswechsel geist-gepingt
#         "gesperrte_tage": {"29.07.2026": "keine Zeit"},  # Tag -> Grund (optional)
#     }, ...
# }
# "tage": { "morteco_heroin_route": { "29.07.2026": { "18:00-19:30": ["uid", ...] } } }
# "gesperrte_user": ["uid", ...]   # globale Routensperre, gilt für ALLE Routen

STANDARD_DATEN = {
    "routen": {},
    "tage": {},
    "gesperrte_user": [],
    "globale_befehle_bereinigt": False,
}

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            geladen = json.load(f)
    else:
        geladen = {}
    if not geladen:
        return dict(STANDARD_DATEN)
    for key, wert in STANDARD_DATEN.items():
        geladen.setdefault(key, wert)
    return geladen

def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

data = load_data()


# ════════════════════════════════════════════════════════════════════════════
# 🤖  BOT SETUP
# ════════════════════════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ════════════════════════════════════════════════════════════════════════════
# 🧰  HILFSFUNKTIONEN
# ════════════════════════════════════════════════════════════════════════════

ZEIT_REGEX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

def heute_key() -> str:
    return datetime.now(TIMEZONE).strftime("%d.%m.%Y")

def parse_datum(datum_str: str) -> str:
    geparst = datetime.strptime(datum_str.strip(), "%d.%m.%Y")
    return geparst.strftime("%d.%m.%Y")

def slot_label(slot: str) -> str:
    start, ende = slot.split("-")
    return f"{start} - {ende} Uhr"

def slot_sortier_schluessel(slot: str) -> int:
    h, m = slot.split("-")[0].split(":")
    return int(h) * 60 + int(m)

def slot_dauer_stunden(slot: str) -> float:
    """Dauer eines Zeitraums in Stunden, inkl. Über-Mitternacht-Slots
    wie '22:30-00:00' (ergibt dann 1.5h statt eines negativen Werts)."""
    start_str, ende_str = slot.split("-")
    start = datetime.strptime(start_str, "%H:%M")
    ende = datetime.strptime(ende_str, "%H:%M")
    diff = (ende - start).total_seconds() / 3600
    if diff <= 0:
        diff += 24
    return diff

def erstelle_route_id(name: str) -> str:
    basis = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "route"
    kandidat = basis
    i = 2
    while kandidat in data.get("routen", {}):
        kandidat = f"{basis}_{i}"
        i += 1
    return kandidat

def route_existiert(route_id: str) -> bool:
    return route_id in data.get("routen", {})

def ist_tag_gesperrt(route_id: str, datum: str) -> bool:
    return datum in data.get("routen", {}).get(route_id, {}).get("gesperrte_tage", {})

def tag_sperrgrund(route_id: str, datum: str):
    return data.get("routen", {}).get(route_id, {}).get("gesperrte_tage", {}).get(datum)

def datum_bereich(start_str: str, end_str: str) -> list:
    """Liste aller Datums-Strings (TT.MM.JJJJ) zwischen start und end (inklusive)."""
    start = datetime.strptime(start_str, "%d.%m.%Y").date()
    ende = datetime.strptime(end_str, "%d.%m.%Y").date()
    if ende < start:
        start, ende = ende, start
    tage = []
    aktuell = start
    while aktuell <= ende:
        tage.append(aktuell.strftime("%d.%m.%Y"))
        aktuell += timedelta(days=1)
    return tage

def get_tag_eintrag(route_id: str, datum: str) -> dict:
    """Holt (oder erstellt) die Slot-Liste einer Route für ein Datum."""
    route_tage = data.setdefault("tage", {}).setdefault(route_id, {})
    eintrag = route_tage.setdefault(datum, {})
    route = data["routen"].get(route_id, {})
    for slot in route.get("slots", []):
        eintrag.setdefault(slot, [])
    return eintrag

def alle_slots_von_user(eintrag: dict, uid: str) -> list:
    return [slot for slot, liste in eintrag.items() if uid in liste]

def gesamt_zeit_pro_user(route_id: str) -> dict:
    """Summiert für jeden User die Stunden auf ABGESCHLOSSENEN Tagen
    (heute und Zukunft zählen bewusst noch nicht mit)."""
    zaehler = {}
    heute = datetime.now(TIMEZONE).date()
    for datum, eintrag in data.get("tage", {}).get(route_id, {}).items():
        try:
            tag_datum = datetime.strptime(datum, "%d.%m.%Y").date()
        except ValueError:
            continue
        if tag_datum >= heute:
            continue
        for slot, liste in eintrag.items():
            dauer = slot_dauer_stunden(slot)
            for uid in liste:
                zaehler[uid] = zaehler.get(uid, 0) + dauer
    return zaehler

def entferne_aus_allen_zukuenftigen_eintraegen(uid: str) -> list:
    """Entfernt uid aus allen HEUTIGEN/ZUKÜNFTIGEN Einträgen (über alle
    Routen hinweg) – wird beim Verhängen einer Routensperre benutzt.
    Vergangene, bereits abgeschlossene Tage bleiben unangetastet.
    Gibt eine Liste von (route_id, datum, slot) zurück, aus denen entfernt wurde."""
    heute = datetime.now(TIMEZONE).date()
    entfernte = []
    for route_id, tage_dict in data.get("tage", {}).items():
        for datum, eintrag in tage_dict.items():
            try:
                tag_datum = datetime.strptime(datum, "%d.%m.%Y").date()
            except ValueError:
                continue
            if tag_datum < heute:
                continue
            for slot, liste in eintrag.items():
                if uid in liste:
                    liste.remove(uid)
                    entfernte.append((route_id, datum, slot))
    return entfernte


# ─── Autocomplete ──────────────────────────────────────────────────────────
async def route_autocomplete(interaction: discord.Interaction, current: str):
    ergebnisse = []
    for rid, info in data.get("routen", {}).items():
        label = f"{info['name']} ({rid})"
        if current.lower() in label.lower():
            ergebnisse.append(app_commands.Choice(name=label[:100], value=rid))
    return ergebnisse[:25]

async def zeitraum_autocomplete(interaction: discord.Interaction, current: str):
    route_id = getattr(interaction.namespace, "route", None)
    route = data.get("routen", {}).get(route_id)
    if not route:
        return []
    heute = heute_key()
    eintrag = get_tag_eintrag(route_id, heute)
    ergebnisse = []
    for slot in sorted(route["slots"], key=slot_sortier_schluessel):
        besetzt = len(eintrag.get(slot, []))
        label = f"{slot_label(slot)} ({besetzt}/{route['kapazitaet']})"
        if current.lower() in slot.lower():
            ergebnisse.append(app_commands.Choice(name=label[:100], value=slot))
    return ergebnisse[:25]


# ════════════════════════════════════════════════════════════════════════════
# 🛣️  ROUTENWACHE-BUTTONS (pro Route eine eigene, persistente Nachricht)
# ════════════════════════════════════════════════════════════════════════════
# WICHTIG (Discord-Limitierung): Eine Buttons-Nachricht sehen alle Mitglieder
# gleich. Ein "für dich voller" Zeitraum kann nicht nur für einzelne Personen
# ausgegraut werden. Volle Zeiträume werden daher ROT eingefärbt, bleiben
# aber klickbar; die Regeln (voll / Routensperre / max. Zeiträume) werden
# beim Klick geprüft, wer nicht darf bekommt eine private Fehlermeldung.

def build_wache_embed(route_id: str, datum: str, guild: discord.Guild) -> discord.Embed:
    route = data["routen"][route_id]
    embed = discord.Embed(title=f"🛣️ {route['name']} – Heute ({datum})", color=EMBED_COLOR)
    eintrag = get_tag_eintrag(route_id, datum)

    if not route["slots"]:
        embed.description = "*Für diese Route sind noch keine Zeiträume konfiguriert.*"
    else:
        bloecke = []
        for slot in sorted(route["slots"], key=slot_sortier_schluessel):
            leute = eintrag.get(slot, [])
            namen = []
            for uid in leute:
                member = guild.get_member(int(uid)) if guild else None
                namen.append(member.mention if member else f"Unbekanntes Mitglied ({uid})")
            text = "\n".join(namen) if namen else "noch unbesetzt"
            voll_hinweis = " 🔒 (voll)" if len(leute) >= route["kapazitaet"] else ""
            bloecke.append(f"**{slot_label(slot)}**{voll_hinweis}\n{text}")
        embed.description = "\n\n".join(bloecke)

    if ist_tag_gesperrt(route_id, datum):
        grund = tag_sperrgrund(route_id, datum)
        grund_text = f" Grund: {grund}" if grund else ""
        banner = f"🚫 **Diese Route ist heute komplett gesperrt.**{grund_text}\n\n"
        embed.description = banner + (embed.description or "")

    embed.set_footer(text=f"ECLIPSE – {route['name']} • Klicke einen Zeitraum an, um dich ein-/auszutragen (max. {route['kapazitaet']} Plätze/Zeitraum)")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed


class WacheView(discord.ui.View):
    """Persistente View mit einem Button pro Zeitraum EINER Route."""

    def __init__(self, route_id: str):
        super().__init__(timeout=None)
        self.route_id = route_id
        self.build_buttons()

    def build_buttons(self):
        self.clear_items()
        route = data["routen"].get(self.route_id)
        if not route:
            return
        today = heute_key()
        eintrag = get_tag_eintrag(self.route_id, today)
        tag_gesperrt = ist_tag_gesperrt(self.route_id, today)
        for slot in sorted(route["slots"], key=slot_sortier_schluessel):
            leute = eintrag.get(slot, [])
            voll = len(leute) >= route["kapazitaet"]
            button = discord.ui.Button(
                label=f"{slot_label(slot)} ({len(leute)}/{route['kapazitaet']})" + (" 🚫" if tag_gesperrt else ""),
                style=discord.ButtonStyle.success if not voll else discord.ButtonStyle.danger,
                disabled=tag_gesperrt,
                custom_id=f"wache_{self.route_id}_{slot}",
            )
            button.callback = self._make_callback(slot)
            self.add_item(button)

    def _make_callback(self, slot: str):
        async def callback(interaction: discord.Interaction):
            await self.handle_click(interaction, slot)
        return callback

    async def handle_click(self, interaction: discord.Interaction, slot: str):
        route = data["routen"].get(self.route_id)
        if not route:
            await interaction.response.send_message("❌ Diese Route existiert nicht mehr.", ephemeral=True)
            return

        uid = str(interaction.user.id)
        today = heute_key()
        eintrag = get_tag_eintrag(self.route_id, today)
        liste = eintrag.setdefault(slot, [])

        # Bereits eingetragen -> IMMER wieder austragen erlaubt, auch wenn voll.
        if uid in liste:
            liste.remove(uid)
            save_data(data)
            self.build_buttons()
            embed = build_wache_embed(self.route_id, today, interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"🔴 Du wurdest aus **{slot_label(slot)}** ausgetragen.", ephemeral=True)
            return

        if uid in data.get("gesperrte_user", []):
            await interaction.response.send_message(
                "❌ Du hast aktuell eine Routensperre und kannst dich nicht eintragen.", ephemeral=True
            )
            return

        if ist_tag_gesperrt(self.route_id, today):
            grund = tag_sperrgrund(self.route_id, today)
            grund_text = f" Grund: {grund}" if grund else ""
            await interaction.response.send_message(f"❌ Diese Route ist heute komplett gesperrt.{grund_text}", ephemeral=True)
            return

        if len(liste) >= route["kapazitaet"]:
            await interaction.response.send_message(
                f"❌ **{slot_label(slot)}** ist bereits voll ({route['kapazitaet']}/{route['kapazitaet']}).",
                ephemeral=True
            )
            return

        aktuelle_slots = alle_slots_von_user(eintrag, uid)
        if len(aktuelle_slots) >= MAX_ZEITRAEUME_PRO_USER:
            vorhandene = ", ".join(f"**{slot_label(s)}**" for s in aktuelle_slots)
            await interaction.response.send_message(
                f"❌ Du bist bereits in {MAX_ZEITRAEUME_PRO_USER} Zeiträumen eingetragen ({vorhandene}). "
                f"Trage dich zuerst aus einem davon aus.",
                ephemeral=True
            )
            return

        liste.append(uid)
        save_data(data)
        self.build_buttons()
        embed = build_wache_embed(self.route_id, today, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"🟢 Du bist eingetragen für **{slot_label(slot)}**!", ephemeral=True)


wache_views: dict = {}  # route_id -> WacheView

async def refresh_wache_nachricht(route_id: str, guild: discord.Guild):
    route = data.get("routen", {}).get(route_id)
    if not route or not route.get("channel_buttons"):
        return
    kanal = guild.get_channel(int(route["channel_buttons"]))
    if not kanal:
        return

    view = wache_views.get(route_id)
    if view is None:
        view = WacheView(route_id)
        wache_views[route_id] = view
    view.build_buttons()

    today = heute_key()
    embed = build_wache_embed(route_id, today, guild)

    msg_id = route.get("stempel_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            print(f"Alte Buttons-Nachricht ({route_id}) nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed, view=view)
    route["stempel_nachricht_id"] = str(msg.id)
    save_data(data)


async def geist_ping_neues_datum(route_id: str, guild: discord.Guild):
    route = data.get("routen", {}).get(route_id)
    if not route or not route.get("channel_buttons") or not route.get("ping_rolle_id"):
        return
    kanal = guild.get_channel(int(route["channel_buttons"]))
    if not kanal:
        return
    rolle = guild.get_role(int(route["ping_rolle_id"]))
    mention_text = rolle.mention if rolle else f"<@&{route['ping_rolle_id']}>"
    try:
        ping_msg = await kanal.send(
            mention_text,
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False)
        )
        await ping_msg.delete()
    except Exception as e:
        print(f"❌ Fehler beim Geist-Ping ({route_id}): {e}")


# ════════════════════════════════════════════════════════════════════════════
# 📋  TAGES-LOG (ein neuer Post pro abgeschlossenem Tag, je Route)
# ════════════════════════════════════════════════════════════════════════════

def build_tages_log_embed(route_id: str, datum: str, guild: discord.Guild) -> discord.Embed:
    route = data["routen"][route_id]
    eintrag = data.get("tage", {}).get(route_id, {}).get(datum, {})

    zeilen = []
    for slot in sorted(eintrag.keys(), key=slot_sortier_schluessel):
        for uid in eintrag.get(slot, []):
            member = guild.get_member(int(uid)) if guild else None
            name = member.mention if member else f"Unbekanntes Mitglied ({uid})"
            zeilen.append(f"{name} — **{slot_label(slot)}**")

    beschreibung = "\n".join(f"{i}. {z}" for i, z in enumerate(zeilen, start=1)) if zeilen else \
        "Niemand war an diesem Tag eingetragen."

    embed = discord.Embed(title=f"📋 {route['name']} – Log ({datum})", description=beschreibung, color=EMBED_COLOR)
    embed.set_footer(text=f"ECLIPSE – {route['name']} • wer wann eingetragen war")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def poste_tages_log(route_id: str, guild: discord.Guild, datum: str):
    route = data.get("routen", {}).get(route_id)
    if not route or not route.get("channel_log"):
        return
    kanal = guild.get_channel(int(route["channel_log"]))
    if not kanal:
        return
    await kanal.send(embed=build_tages_log_embed(route_id, datum, guild))


# ════════════════════════════════════════════════════════════════════════════
# 📊  GESAMTÜBERSICHT / LEADERBOARD (pro Route, nur abgeschlossene Tage)
# ════════════════════════════════════════════════════════════════════════════

def build_gesamtuebersicht_embed(route_id: str, guild: discord.Guild) -> discord.Embed:
    route = data["routen"][route_id]
    zaehler = gesamt_zeit_pro_user(route_id)

    if not zaehler:
        beschreibung = "*Noch keine abgeschlossenen Wachen erfasst.*"
    else:
        sortiert = sorted(zaehler.items(), key=lambda x: x[1], reverse=True)
        zeilen = []
        for i, (uid, stunden) in enumerate(sortiert, start=1):
            member = guild.get_member(int(uid)) if guild else None
            name = member.mention if member else f"Unbekanntes Mitglied ({uid})"
            stunden_text = f"{stunden:.1f}".rstrip("0").rstrip(".")
            zeilen.append(f"**{i}.** {name} — **{stunden_text}h**")
        beschreibung = "\n".join(zeilen)
        if len(beschreibung) > 4000:
            beschreibung = beschreibung[:4000] + "\n… (gekürzt)"

    embed = discord.Embed(title=f"📊 Gesamtübersicht – {route['name']}", description=beschreibung, color=EMBED_COLOR)
    embed.set_footer(text="ECLIPSE – Summe aller abgeschlossenen Tage • täglich 00:01 Uhr aktualisiert")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def refresh_gesamtuebersicht(route_id: str, guild: discord.Guild):
    route = data.get("routen", {}).get(route_id)
    if not route or not route.get("channel_leaderboard"):
        return
    kanal = guild.get_channel(int(route["channel_leaderboard"]))
    if not kanal:
        return

    embed = build_gesamtuebersicht_embed(route_id, guild)
    msg_id = route.get("leaderboard_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
            return
        except Exception as e:
            print(f"Alte Leaderboard-Nachricht ({route_id}) nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed)
    route["leaderboard_nachricht_id"] = str(msg.id)
    save_data(data)


# ════════════════════════════════════════════════════════════════════════════
# 🎛️  SLASH-COMMANDS — ROUTEN-VERWALTUNG (nur Leitung/Admin)
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="route_erstellen", description="Erstellt eine neue Route (z.B. 'SGF Kurzwaffen Route')")
@app_commands.describe(
    name="Name der Route",
    kapazitaet="Max. Personen pro Zeitraum",
    standard_zeitraeume="Die Morteco-Zeiträume (18-19:30, 19:30-21, 21-22:30, 22:30-00 Uhr) automatisch übernehmen? Standard: Ja"
)
@app_commands.check(ist_admin_oder_leitung)
async def route_erstellen(interaction: discord.Interaction, name: str, kapazitaet: int, standard_zeitraeume: bool = True):
    if kapazitaet < 1:
        await interaction.response.send_message("❌ Kapazität muss mindestens 1 sein.", ephemeral=True)
        return
    route_id = erstelle_route_id(name)
    data.setdefault("routen", {})[route_id] = {
        "name": name,
        "kapazitaet": kapazitaet,
        "slots": list(STANDARD_SLOTS) if standard_zeitraeume else [],
        "channel_buttons": None,
        "stempel_nachricht_id": None,
        "channel_log": None,
        "channel_leaderboard": None,
        "leaderboard_nachricht_id": None,
        "ping_rolle_id": None,
        "gesperrte_tage": {},
    }
    save_data(data)
    zeitraeume_text = (
        "\nÜbernommene Zeiträume: " + ", ".join(slot_label(s) for s in STANDARD_SLOTS)
        if standard_zeitraeume else
        "\nNoch keine Zeiträume – füge sie mit `/route_slot_hinzufuegen` hinzu."
    )
    await interaction.response.send_message(
        f"✅ Route **{name}** erstellt (ID: `{route_id}`).{zeitraeume_text}\n"
        f"Als Nächstes: `/route_channel_buttons_setzen` usw.",
        ephemeral=True
    )

@tree.command(name="route_loeschen", description="Löscht eine Route komplett (inkl. aller erfassten Zeiten)")
@app_commands.describe(route="Die zu löschende Route")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_loeschen(interaction: discord.Interaction, route: str):
    info = data.get("routen", {}).get(route)
    if not info:
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return

    if info.get("channel_buttons") and info.get("stempel_nachricht_id"):
        try:
            kanal = interaction.guild.get_channel(int(info["channel_buttons"]))
            if kanal:
                msg = await kanal.fetch_message(int(info["stempel_nachricht_id"]))
                await msg.delete()
        except Exception:
            pass

    name = info["name"]
    del data["routen"][route]
    data.get("tage", {}).pop(route, None)
    wache_views.pop(route, None)
    save_data(data)
    await interaction.response.send_message(f"🗑️ Route **{name}** (`{route}`) und alle zugehörigen Daten wurden gelöscht.", ephemeral=True)

@tree.command(name="route_slot_hinzufuegen", description="Fügt einer Route einen Zeitraum hinzu (z.B. 18:00 bis 19:30)")
@app_commands.describe(route="Die Route", start="Startzeit HH:MM, z.B. 18:00", ende="Endzeit HH:MM, z.B. 19:30")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_slot_hinzufuegen(interaction: discord.Interaction, route: str, start: str, ende: str):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    start, ende = start.strip(), ende.strip()
    if not ZEIT_REGEX.match(start) or not ZEIT_REGEX.match(ende):
        await interaction.response.send_message("❌ Ungültiges Zeitformat. Bitte HH:MM verwenden, z.B. `18:00`.", ephemeral=True)
        return

    slot = f"{start}-{ende}"
    slots = data["routen"][route]["slots"]
    if slot in slots:
        await interaction.response.send_message(f"❌ Zeitraum **{slot_label(slot)}** existiert bereits.", ephemeral=True)
        return

    slots.append(slot)
    save_data(data)
    await interaction.response.send_message(f"✅ Zeitraum **{slot_label(slot)}** zur Route hinzugefügt.", ephemeral=True)
    await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="route_slot_entfernen", description="Entfernt einen Zeitraum von einer Route")
@app_commands.describe(route="Die Route", zeitraum="Der zu entfernende Zeitraum")
@app_commands.autocomplete(route=route_autocomplete, zeitraum=zeitraum_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_slot_entfernen(interaction: discord.Interaction, route: str, zeitraum: str):
    slots = data.get("routen", {}).get(route, {}).get("slots", [])
    if zeitraum not in slots:
        await interaction.response.send_message("❌ Dieser Zeitraum existiert bei dieser Route nicht.", ephemeral=True)
        return
    slots.remove(zeitraum)
    save_data(data)
    await interaction.response.send_message(f"✅ Zeitraum **{slot_label(zeitraum)}** entfernt.", ephemeral=True)
    await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="route_kapazitaet_setzen", description="Setzt die maximale Personenzahl pro Zeitraum einer Route")
@app_commands.describe(route="Die Route", kapazitaet="Neue max. Personenzahl pro Zeitraum")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_kapazitaet_setzen(interaction: discord.Interaction, route: str, kapazitaet: int):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    if kapazitaet < 1:
        await interaction.response.send_message("❌ Kapazität muss mindestens 1 sein.", ephemeral=True)
        return
    data["routen"][route]["kapazitaet"] = kapazitaet
    save_data(data)
    await interaction.response.send_message(f"✅ Kapazität auf **{kapazitaet}** pro Zeitraum gesetzt.", ephemeral=True)
    await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="route_tag_sperren", description="Sperrt einen (oder mehrere) Tag(e) für eine Route komplett, z.B. wenn keine Zeit ist")
@app_commands.describe(
    route="Die Route",
    datum="Datum TT.MM.JJJJ (Standard: heute)",
    bis="Optional: Enddatum TT.MM.JJJJ, um einen Zeitraum zu sperren",
    grund="Optional: Grund, z.B. 'keine Zeit' (wird in der Nachricht angezeigt)"
)
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_tag_sperren(interaction: discord.Interaction, route: str, datum: str = None, bis: str = None, grund: str = None):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    try:
        start_tag = parse_datum(datum) if datum else heute_key()
        tage_liste = datum_bereich(start_tag, parse_datum(bis)) if bis else [start_tag]
    except ValueError:
        await interaction.response.send_message("❌ Ungültiges Datum. Format: **TT.MM.JJJJ**, z.B. `27.07.2026`.", ephemeral=True)
        return

    routeninfo = data["routen"][route]
    gesperrte_tage = routeninfo.setdefault("gesperrte_tage", {})
    entfernte_eintraege = 0
    for tag in tage_liste:
        gesperrte_tage[tag] = grund
        eintrag = data.get("tage", {}).get(route, {}).get(tag, {})
        for slot, liste in eintrag.items():
            entfernte_eintraege += len(liste)
            liste.clear()
    save_data(data)

    grund_text = f" Grund: {grund}" if grund else ""
    tage_text = f"**{tage_liste[0]}**" if len(tage_liste) == 1 else f"**{tage_liste[0]}** bis **{tage_liste[-1]}** ({len(tage_liste)} Tage)"
    zusatz = f"\n{entfernte_eintraege} bestehende Einträge wurden dabei entfernt." if entfernte_eintraege else ""
    await interaction.response.send_message(f"🚫 **{routeninfo['name']}** ist an {tage_text} gesperrt.{grund_text}{zusatz}", ephemeral=True)

    if heute_key() in tage_liste:
        await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="route_tag_entsperren", description="Hebt die Sperre für einen (oder mehrere) Tag(e) einer Route wieder auf")
@app_commands.describe(
    route="Die Route",
    datum="Datum TT.MM.JJJJ (Standard: heute)",
    bis="Optional: Enddatum TT.MM.JJJJ, um einen Zeitraum zu entsperren"
)
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_tag_entsperren(interaction: discord.Interaction, route: str, datum: str = None, bis: str = None):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    try:
        start_tag = parse_datum(datum) if datum else heute_key()
        tage_liste = datum_bereich(start_tag, parse_datum(bis)) if bis else [start_tag]
    except ValueError:
        await interaction.response.send_message("❌ Ungültiges Datum. Format: **TT.MM.JJJJ**, z.B. `27.07.2026`.", ephemeral=True)
        return

    routeninfo = data["routen"][route]
    gesperrte_tage = routeninfo.setdefault("gesperrte_tage", {})
    entsperrt = 0
    for tag in tage_liste:
        if tag in gesperrte_tage:
            del gesperrte_tage[tag]
            entsperrt += 1
    save_data(data)

    if entsperrt == 0:
        await interaction.response.send_message("❌ Für diesen Zeitraum war keine Sperre aktiv.", ephemeral=True)
        return

    tage_text = f"**{tage_liste[0]}**" if len(tage_liste) == 1 else f"**{tage_liste[0]}** bis **{tage_liste[-1]}**"
    await interaction.response.send_message(f"✅ Sperre für **{routeninfo['name']}** an {tage_text} aufgehoben ({entsperrt} Tag(e)).", ephemeral=True)

    if heute_key() in tage_liste:
        await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="route_channel_buttons_setzen", description="Setzt den Channel für die Ein-/Austragen-Buttons einer Route")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_channel_buttons_setzen(interaction: discord.Interaction, route: str, channel: discord.TextChannel):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    data["routen"][route]["channel_buttons"] = channel.id
    data["routen"][route]["stempel_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Buttons-Channel gesetzt: {channel.mention}", ephemeral=True)
    await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="route_channel_log_setzen", description="Setzt den Channel für das tägliche Log (00:01 Uhr) einer Route")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_channel_log_setzen(interaction: discord.Interaction, route: str, channel: discord.TextChannel):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    data["routen"][route]["channel_log"] = channel.id
    save_data(data)
    await interaction.response.send_message(f"✅ Log-Channel gesetzt: {channel.mention}", ephemeral=True)

@tree.command(name="route_channel_leaderboard_setzen", description="Setzt den Channel für die Gesamtübersicht einer Route")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_channel_leaderboard_setzen(interaction: discord.Interaction, route: str, channel: discord.TextChannel):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    data["routen"][route]["channel_leaderboard"] = channel.id
    data["routen"][route]["leaderboard_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Leaderboard-Channel gesetzt: {channel.mention}", ephemeral=True)
    await refresh_gesamtuebersicht(route, interaction.guild)

@tree.command(name="route_ping_rolle_setzen", description="Setzt eine Rolle, die beim Tageswechsel geist-gepingt wird (optional)")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_ping_rolle_setzen(interaction: discord.Interaction, route: str, rolle: discord.Role):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    data["routen"][route]["ping_rolle_id"] = rolle.id
    save_data(data)
    await interaction.response.send_message(f"✅ Ping-Rolle gesetzt: {rolle.mention}", ephemeral=True)

@tree.command(name="route_ping_rolle_entfernen", description="Entfernt die Geist-Ping-Rolle einer Route wieder")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_ping_rolle_entfernen(interaction: discord.Interaction, route: str):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    data["routen"][route]["ping_rolle_id"] = None
    save_data(data)
    await interaction.response.send_message("✅ Ping-Rolle entfernt.", ephemeral=True)

@tree.command(name="route_posten", description="Postet/aktualisiert die Buttons-Nachricht einer Route")
@app_commands.autocomplete(route=route_autocomplete)
@app_commands.check(ist_admin_oder_leitung)
async def route_posten(interaction: discord.Interaction, route: str):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await refresh_wache_nachricht(route, interaction.guild)
    await interaction.followup.send("✅ Buttons-Nachricht gepostet/aktualisiert.", ephemeral=True)

@tree.command(name="routen_liste", description="Zeigt alle konfigurierten Routen mit ihren Einstellungen")
@app_commands.check(ist_admin_oder_leitung)
async def routen_liste(interaction: discord.Interaction):
    routen = data.get("routen", {})
    if not routen:
        await interaction.response.send_message("Es sind noch keine Routen konfiguriert. Nutze `/route_erstellen`.", ephemeral=True)
        return

    embed = discord.Embed(title="🗺️ Konfigurierte Routen", color=EMBED_COLOR)
    for rid, info in routen.items():
        slots_text = ", ".join(slot_label(s) for s in sorted(info["slots"], key=slot_sortier_schluessel)) or "*keine Zeiträume*"
        btn_ch = f"<#{info['channel_buttons']}>" if info.get("channel_buttons") else "❌"
        log_ch = f"<#{info['channel_log']}>" if info.get("channel_log") else "❌"
        lb_ch = f"<#{info['channel_leaderboard']}>" if info.get("channel_leaderboard") else "❌"
        gesperrte_tage = info.get("gesperrte_tage", {})
        sperr_text = f"\nGesperrte Tage: {', '.join(sorted(gesperrte_tage.keys()))}" if gesperrte_tage else ""
        embed.add_field(
            name=f"{info['name']} (`{rid}`)",
            value=f"Kapazität: **{info['kapazitaet']}**/Zeitraum\nZeiträume: {slots_text}\nButtons: {btn_ch} • Log: {log_ch} • Leaderboard: {lb_ch}{sperr_text}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ════════════════════════════════════════════════════════════════════════════
# 🎛️  SLASH-COMMANDS — EIN-/AUSTRAGEN (offen für ALLE)
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="wache_eintragen", description="Trägt dich (oder ein Mitglied) für einen Zeitraum einer Route ein")
@app_commands.describe(
    route="Die Route", zeitraum="Der Zeitraum",
    mitglied="Optional: anderes Mitglied eintragen (Standard: du selbst)",
    datum="Optional: Datum TT.MM.JJJJ, z.B. für zukünftige Tage (Standard: heute)"
)
@app_commands.autocomplete(route=route_autocomplete, zeitraum=zeitraum_autocomplete)
async def wache_eintragen(interaction: discord.Interaction, route: str, zeitraum: str, mitglied: discord.Member = None, datum: str = None):
    routeninfo = data.get("routen", {}).get(route)
    if not routeninfo:
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    if zeitraum not in routeninfo["slots"]:
        await interaction.response.send_message("❌ Dieser Zeitraum existiert bei dieser Route nicht (mehr).", ephemeral=True)
        return

    ziel = mitglied or interaction.user
    uid = str(ziel.id)
    if uid in data.get("gesperrte_user", []):
        await interaction.response.send_message(f"❌ {ziel.mention} hat aktuell eine Routensperre.", ephemeral=True)
        return

    if datum:
        try:
            tag = parse_datum(datum)
        except ValueError:
            await interaction.response.send_message("❌ Ungültiges Datum. Format: **TT.MM.JJJJ**, z.B. `27.07.2026`.", ephemeral=True)
            return
    else:
        tag = heute_key()

    if ist_tag_gesperrt(route, tag):
        grund = tag_sperrgrund(route, tag)
        grund_text = f" Grund: {grund}" if grund else ""
        await interaction.response.send_message(f"❌ **{tag}** ist für diese Route komplett gesperrt.{grund_text}", ephemeral=True)
        return

    eintrag = get_tag_eintrag(route, tag)
    liste = eintrag.setdefault(zeitraum, [])

    if uid in liste:
        await interaction.response.send_message(f"❌ {ziel.mention} ist am **{tag}** bereits für **{slot_label(zeitraum)}** eingetragen.", ephemeral=True)
        return
    if len(liste) >= routeninfo["kapazitaet"]:
        await interaction.response.send_message(f"❌ **{tag} — {slot_label(zeitraum)}** ist bereits voll ({routeninfo['kapazitaet']}/{routeninfo['kapazitaet']}).", ephemeral=True)
        return
    aktuelle = alle_slots_von_user(eintrag, uid)
    if len(aktuelle) >= MAX_ZEITRAEUME_PRO_USER:
        await interaction.response.send_message(f"❌ {ziel.mention} ist am **{tag}** bereits in {MAX_ZEITRAEUME_PRO_USER} Zeiträumen dieser Route eingetragen.", ephemeral=True)
        return

    liste.append(uid)
    save_data(data)
    await interaction.response.send_message(f"✅ {ziel.mention} wurde für **{tag} — {slot_label(zeitraum)}** ({routeninfo['name']}) eingetragen.", ephemeral=True)

    if tag == heute_key():
        await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="wache_austragen", description="Trägt dich (oder ein Mitglied) aus einem oder allen Zeiträumen einer Route aus")
@app_commands.describe(
    route="Die Route",
    mitglied="Optional: anderes Mitglied austragen (Standard: du selbst)",
    zeitraum="Optional: nur aus diesem Zeitraum austragen (Standard: alle Zeiträume dieses Tages)",
    datum="Optional: Datum TT.MM.JJJJ, z.B. für einen zukünftigen/vergangenen Tag (Standard: heute)"
)
@app_commands.autocomplete(route=route_autocomplete, zeitraum=zeitraum_autocomplete)
async def wache_austragen(interaction: discord.Interaction, route: str, mitglied: discord.Member = None, zeitraum: str = None, datum: str = None):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    ziel = mitglied or interaction.user

    if datum:
        try:
            tag = parse_datum(datum)
        except ValueError:
            await interaction.response.send_message("❌ Ungültiges Datum. Format: **TT.MM.JJJJ**, z.B. `27.07.2026`.", ephemeral=True)
            return
    else:
        tag = heute_key()

    eintrag = get_tag_eintrag(route, tag)
    uid = str(ziel.id)

    if zeitraum:
        zu_entfernen = [zeitraum] if uid in eintrag.get(zeitraum, []) else []
    else:
        zu_entfernen = alle_slots_von_user(eintrag, uid)

    if not zu_entfernen:
        bezug = f"für **{slot_label(zeitraum)}**" if zeitraum else "für keinen Zeitraum"
        await interaction.response.send_message(f"❌ {ziel.mention} ist am **{tag}** {bezug} eingetragen.", ephemeral=True)
        return

    for slot in zu_entfernen:
        eintrag[slot].remove(uid)
    save_data(data)

    zeitraeume_text = ", ".join(f"**{slot_label(s)}**" for s in zu_entfernen)
    await interaction.response.send_message(f"✅ {ziel.mention} wurde am **{tag}** aus {zeitraeume_text} ausgetragen.", ephemeral=True)

    if tag == heute_key():
        await refresh_wache_nachricht(route, interaction.guild)

@tree.command(name="meine_wache", description="Zeigt deinen heutigen Routenwache-Status (alle Routen oder eine bestimmte)")
@app_commands.describe(route="Optional: nur eine bestimmte Route anzeigen")
@app_commands.autocomplete(route=route_autocomplete)
async def meine_wache(interaction: discord.Interaction, route: str = None):
    today = heute_key()
    uid = str(interaction.user.id)
    routen = {route: data["routen"][route]} if route and route in data.get("routen", {}) else data.get("routen", {})

    zeilen = []
    for rid, info in routen.items():
        eintrag = get_tag_eintrag(rid, today)
        slots = alle_slots_von_user(eintrag, uid)
        for slot in slots:
            zeilen.append(f"🟢 **{info['name']}** — {slot_label(slot)}")

    text = "\n".join(zeilen) if zeilen else "🔴 Du bist heute für keinen Zeitraum eingetragen."
    gesperrt_hinweis = "\n\n🔒 Du hast aktuell eine Routensperre." if uid in data.get("gesperrte_user", []) else ""
    await interaction.response.send_message(f"**Deine Routenwache ({today})**\n{text}{gesperrt_hinweis}", ephemeral=True)

@tree.command(name="route_gesamtuebersicht", description="Zeigt, wer bei einer Route insgesamt wie viele Stunden gemacht hat")
@app_commands.describe(route="Die Route")
@app_commands.autocomplete(route=route_autocomplete)
async def route_gesamtuebersicht(interaction: discord.Interaction, route: str):
    if route not in data.get("routen", {}):
        await interaction.response.send_message("❌ Route nicht gefunden.", ephemeral=True)
        return
    embed = build_gesamtuebersicht_embed(route, interaction.guild)
    await interaction.response.send_message(embed=embed)


# ════════════════════════════════════════════════════════════════════════════
# 🎛️  SLASH-COMMANDS — ROUTENSPERRE (nur Leitung/Admin)
# ════════════════════════════════════════════════════════════════════════════

@tree.command(name="routensperre_setzen", description="Verhängt eine Routensperre über ein Mitglied (kann sich nirgends mehr eintragen)")
@app_commands.describe(mitglied="Das Mitglied", grund="Optional: Grund für die Sperre (nur zur Dokumentation)")
@app_commands.check(ist_admin_oder_leitung)
async def routensperre_setzen(interaction: discord.Interaction, mitglied: discord.Member, grund: str = None):
    uid = str(mitglied.id)
    gesperrte = data.setdefault("gesperrte_user", [])
    if uid in gesperrte:
        await interaction.response.send_message(f"❌ {mitglied.mention} ist bereits gesperrt.", ephemeral=True)
        return

    gesperrte.append(uid)
    entfernte = entferne_aus_allen_zukuenftigen_eintraegen(uid)
    save_data(data)

    grund_text = f"\nGrund: {grund}" if grund else ""
    zusatz = f"\nAutomatisch aus {len(entfernte)} bevorstehenden Einträgen ausgetragen." if entfernte else ""
    await interaction.response.send_message(f"🔒 {mitglied.mention} wurde gesperrt.{grund_text}{zusatz}", ephemeral=True)

    heute = heute_key()
    betroffene_routen = {r for (r, d, s) in entfernte if d == heute}
    for r in betroffene_routen:
        await refresh_wache_nachricht(r, interaction.guild)

@tree.command(name="routensperre_aufheben", description="Hebt die Routensperre eines Mitglieds wieder auf")
@app_commands.describe(mitglied="Das Mitglied")
@app_commands.check(ist_admin_oder_leitung)
async def routensperre_aufheben(interaction: discord.Interaction, mitglied: discord.Member):
    uid = str(mitglied.id)
    gesperrte = data.setdefault("gesperrte_user", [])
    if uid not in gesperrte:
        await interaction.response.send_message(f"❌ {mitglied.mention} ist gar nicht gesperrt.", ephemeral=True)
        return
    gesperrte.remove(uid)
    save_data(data)
    await interaction.response.send_message(f"🔓 Routensperre von {mitglied.mention} wurde aufgehoben.", ephemeral=True)


@tree.command(name="channels", description="Zeigt alle Routen mit ihren gesetzten Channels")
@app_commands.check(ist_admin_oder_leitung)
async def channels_info(interaction: discord.Interaction):
    await routen_liste.callback(interaction)

@tree.command(name="sync_status", description="Zeigt, ob GUILD_ID gesetzt ist und wie die Befehle gesynct wurden")
@app_commands.check(ist_admin_oder_leitung)
async def sync_status(interaction: discord.Interaction):
    guild_id_status = f"✅ Gesetzt: `{GUILD_ID}`" if GUILD_ID else "❌ NICHT gesetzt (Railway-Variable fehlt!)"
    passt = ""
    if GUILD_ID and interaction.guild:
        passt = " ✅ (passt zu diesem Server)" if str(interaction.guild.id) == str(GUILD_ID) else " ⚠️ (passt NICHT zu diesem Server!)"
    await interaction.response.send_message(
        f"**GUILD_ID:** {guild_id_status}{passt}\n"
        f"**Diese Server-ID:** `{interaction.guild.id if interaction.guild else '-'}`\n"
        f"**Globale Bereinigung schon gelaufen:** {'✅ Ja' if data.get('globale_befehle_bereinigt') else '❌ Nein'}",
        ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════════════
# 🌙  TAGESWECHSEL (automatisch täglich um 00:01 Uhr, für ALLE Routen)
# ════════════════════════════════════════════════════════════════════════════
letzter_bekannter_tag = None

@tasks.loop(time=dt_time(hour=0, minute=1, tzinfo=TIMEZONE))
async def tageswechsel_check():
    global letzter_bekannter_tag
    vorheriger_tag = letzter_bekannter_tag
    heute = heute_key()
    letzter_bekannter_tag = heute

    for guild in bot.guilds:
        for route_id in list(data.get("routen", {}).keys()):
            try:
                if vorheriger_tag and vorheriger_tag != heute:
                    await poste_tages_log(route_id, guild, vorheriger_tag)
                await refresh_wache_nachricht(route_id, guild)
                await refresh_gesamtuebersicht(route_id, guild)
                if vorheriger_tag and vorheriger_tag != heute:
                    await geist_ping_neues_datum(route_id, guild)
            except Exception as e:
                print(f"❌ Fehler beim Tageswechsel ({route_id}): {e}")
    print(f"🌙 00:01 Tageswechsel für {len(data.get('routen', {}))} Route(n) verarbeitet.")

@tageswechsel_check.before_loop
async def before_tageswechsel_check():
    await bot.wait_until_ready()


# ════════════════════════════════════════════════════════════════════════════
# 🎯  BOT EVENTS
# ════════════════════════════════════════════════════════════════════════════

async def sync_commands():
    if not GUILD_ID:
        try:
            synced = await tree.sync()
            print(f"⚠️ Keine GUILD_ID gesetzt — {len(synced)} Commands global gesynct (kann bis zu 1h dauern).")
        except Exception as e:
            print(f"❌ FEHLER beim globalen Sync: {e}")
        return

    try:
        guild_obj = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)
        print(f"✅ {len(synced)} Commands sofort auf Guild {GUILD_ID} gesynct: {[c.name for c in synced]}")
    except Exception as e:
        print(f"❌ FEHLER beim Guild-Sync: {e}")
        return

    if not data.get("globale_befehle_bereinigt"):
        try:
            tree.clear_commands(guild=None)
            await tree.sync()
            data["globale_befehle_bereinigt"] = True
            save_data(data)
            print("🧹 Alte globale Befehle einmalig bereinigt.")
        except Exception as e:
            print(f"⚠️ Konnte globale Befehle nicht bereinigen: {e}")


@bot.event
async def on_ready():
    global letzter_bekannter_tag
    print(f"Bot online: {bot.user}")

    for route_id in data.get("routen", {}):
        if route_id not in wache_views:
            wache_views[route_id] = WacheView(route_id)
        bot.add_view(wache_views[route_id])

    await sync_commands()
    letzter_bekannter_tag = heute_key()

    for guild in bot.guilds:
        for route_id in list(data.get("routen", {}).keys()):
            try:
                await refresh_wache_nachricht(route_id, guild)
                await refresh_gesamtuebersicht(route_id, guild)
            except Exception as e:
                print(f"❌ Fehler beim Auto-Posten ({route_id}): {e}")

    if not tageswechsel_check.is_running():
        tageswechsel_check.start()

    print("Bot ist bereit!")


@bot.event
async def on_member_remove(member: discord.Member):
    """Entfernt automatisch die HEUTIGEN Einträge eines Mitglieds über
    ALLE Routen hinweg, sobald es den Server verlässt. Abgeschlossene
    Tage bleiben für die Gesamtübersicht erhalten."""
    uid = str(member.id)
    heute = heute_key()
    betroffene_routen = []

    for route_id in data.get("routen", {}):
        eintrag = data.get("tage", {}).get(route_id, {}).get(heute, {})
        for slot, liste in eintrag.items():
            if uid in liste:
                liste.remove(uid)
                if route_id not in betroffene_routen:
                    betroffene_routen.append(route_id)

    if not betroffene_routen:
        return

    save_data(data)
    print(f"🧹 Heutige Einträge von {member} ({uid}) entfernt (Server verlassen).")
    for route_id in betroffene_routen:
        try:
            await refresh_wache_nachricht(route_id, member.guild)
        except Exception as e:
            print(f"❌ Fehler beim Aktualisieren nach Austritt ({route_id}): {e}")


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
    else:
        print(f"Command Error: {error}")
        try:
            await interaction.response.send_message("Ein Fehler ist aufgetreten.", ephemeral=True)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# 🚀  START
# ════════════════════════════════════════════════════════════════════════════
bot.run(TOKEN)
