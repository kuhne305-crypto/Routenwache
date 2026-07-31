import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, time as dt_time
import pytz
import json
import os

# ════════════════════════════════════════════════════════════════════════════
# ⚙️  KONFIGURATION
# ════════════════════════════════════════════════════════════════════════════
TOKEN    = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
TIMEZONE = pytz.timezone("Europe/Berlin")
EMBED_COLOR = 0xFFD700  # Gelb
DATA_DIR  = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Feste Zeiträume der Routenwache, jeweils max. 3 Plätze.
SLOTS = ["20-21", "21-22", "22-23", "23-24"]
MAX_PLAETZE_PRO_SLOT = 3

# Leitung: darf das Setup (Channel setzen, Nachricht posten, Nachtragen) erledigen.
# Ein-/Austragen in einen Zeitraum ist bewusst für ALLE offen.
LEITUNG_ROLLE_ID = 1526202327483285629

# Alle Mitglieder mit dieser Rolle erscheinen im Leaderboard – auch mit 0 Stunden.
ROUTENWACHE_ROLLE_ID = 1526202327365582918


def ist_admin_oder_leitung(interaction: discord.Interaction) -> bool:
    """True für echte Admins ODER Mitglieder mit der Leitungs-Rolle.
    Wird nur für die Setup-/Korrektur-Befehle benutzt."""
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == LEITUNG_ROLLE_ID for r in interaction.user.roles)


# ════════════════════════════════════════════════════════════════════════════
# 💾  DATENSPEICHER
# ════════════════════════════════════════════════════════════════════════════
# Hinweis: Die Feldnamen bleiben bewusst so, wie sie schon in data.json auf
# dem Server stehen (channel_stempel, channel_stempel_liste,
# channel_gesamtuebersicht, ...) – damit beim Deploy nichts von der
# bestehenden Konfiguration (bereits gesetzte Channels) verloren geht.

STANDARD_DATEN = {
    "channel_stempel": 1531376112226140260,          # Buttons-Channel (Eintragen)
    "stempel_nachricht_id": None,
    "channel_stempel_liste": 1531376274130341909,     # Log-Channel (täglicher Post um 00:01 Uhr)
    "channel_gesamtuebersicht": None,                 # Leaderboard-Channel (/set_leaderboard)
    "gesamtuebersicht_nachricht_id": None,
    "tage": {},  # { "28.07.2026": { "20-21": ["userid", ...], "21-22": [...], ... } }
    "globale_befehle_bereinigt": False,  # wird nach der einmaligen Bereinigung auf True gesetzt
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

def heute_key() -> str:
    """Aktuelles Datum als String, z.B. '29.07.2026'."""
    return datetime.now(TIMEZONE).strftime("%d.%m.%Y")

def parse_datum(datum_str: str) -> str:
    """Validiert eine Nutzereingabe im Format TT.MM.JJJJ und gibt sie
    normalisiert zurück (z.B. '3.7.2026' -> '03.07.2026').
    Wirft ValueError bei ungültigem Format."""
    geparst = datetime.strptime(datum_str.strip(), "%d.%m.%Y")
    return geparst.strftime("%d.%m.%Y")

def slot_label(slot: str) -> str:
    start, ende = slot.split("-")
    return f"{start} - {ende} Uhr"

def get_tag_eintrag(datum: str) -> dict:
    """Holt (oder erstellt) die Slot-Liste für ein bestimmtes Datum."""
    tage = data.setdefault("tage", {})
    eintrag = tage.setdefault(datum, {})
    for slot in SLOTS:
        eintrag.setdefault(slot, [])
    return eintrag

def alle_slots_von_user(eintrag: dict, uid: str) -> list:
    """Gibt ALLE Slots zurück, in denen uid an diesem Tag eingetragen ist
    (kann mehrere sein, da Mehrfach-Eintragung erlaubt ist)."""
    return [slot for slot, liste in eintrag.items() if uid in liste]

def gesamt_zeit_pro_user() -> dict:
    """Zählt für jeden User, in wie vielen Zeitraum-Slots (= Stunden) er
    insgesamt eingetragen war – aber NUR über bereits ABGESCHLOSSENE Tage.
    Der heutige, noch laufende Tag zählt bewusst noch nicht mit, da er
    sich noch ändern kann (Ein-/Austragen läuft ja noch)."""
    zaehler = {}
    heute = heute_key()
    for datum, eintrag in data.get("tage", {}).items():
        if datum == heute:
            continue
        for slot, liste in eintrag.items():
            for uid in liste:
                zaehler[uid] = zaehler.get(uid, 0) + 1
    return zaehler


# ════════════════════════════════════════════════════════════════════════════
# 🛣️  ROUTENWACHE-BUTTONS (Eintragen für den heutigen Tag)
# ════════════════════════════════════════════════════════════════════════════
# Komplett offen: es gibt hier absichtlich KEINE Rollen-/Berechtigungs-
# einschränkung. Jedes Mitglied kann sich für einen oder mehrere Zeiträume
# ein-/austragen.

def build_wache_embed(datum: str, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title=f"🛣️ Routenwache Heute ({datum})", color=EMBED_COLOR)
    eintrag = get_tag_eintrag(datum)

    bloecke = []
    for slot in SLOTS:
        leute = eintrag.get(slot, [])
        namen = []
        for uid in leute:
            member = guild.get_member(int(uid)) if guild else None
            namen.append(member.mention if member else f"Unbekanntes Mitglied ({uid})")
        text = "\n".join(namen) if namen else "noch unbesetzt"
        voll_hinweis = " 🔒 (voll)" if len(leute) >= MAX_PLAETZE_PRO_SLOT else ""
        bloecke.append(f"**{slot_label(slot)}**{voll_hinweis}\n{text}")

    embed.description = "\n\n".join(bloecke)
    embed.set_footer(text="ECLIPSE – Routenwache • Klicke einen oder mehrere Zeiträume an, um dich ein- oder wieder auszutragen (max. 3 Plätze pro Stunde)")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed


class WacheView(discord.ui.View):
    """Persistente View mit einem Button pro Zeitraum. Wird dynamisch neu
    aufgebaut, damit volle Zeiträume ausgegraut/deaktiviert sind.

    Jeder Zeitraum-Button ist unabhängig: man kann sich für beliebig viele
    Zeiträume gleichzeitig eintragen. Klickt man den Button eines Zeitraums
    an, in dem man selbst schon eingetragen ist, wird man dort wieder
    ausgetragen (Toggle) – die anderen Eintragungen bleiben unberührt."""

    def __init__(self):
        super().__init__(timeout=None)
        self.build_buttons()

    def build_buttons(self):
        self.clear_items()
        today = heute_key()
        eintrag = get_tag_eintrag(today)
        for slot in SLOTS:
            leute = eintrag.get(slot, [])
            voll = len(leute) >= MAX_PLAETZE_PRO_SLOT
            button = discord.ui.Button(
                label=f"{slot_label(slot)} ({len(leute)}/{MAX_PLAETZE_PRO_SLOT})",
                style=discord.ButtonStyle.success if not voll else discord.ButtonStyle.secondary,
                disabled=voll,
                custom_id=f"wache_slot_{slot}",
            )
            button.callback = self._make_callback(slot)
            self.add_item(button)

    def _make_callback(self, slot: str):
        async def callback(interaction: discord.Interaction):
            await self.handle_click(interaction, slot)
        return callback

    async def handle_click(self, interaction: discord.Interaction, slot: str):
        today = heute_key()
        eintrag = get_tag_eintrag(today)
        uid = str(interaction.user.id)
        liste = eintrag.setdefault(slot, [])

        # Bereits in DIESEM Zeitraum eingetragen -> wieder austragen
        if uid in liste:
            liste.remove(uid)
            save_data(data)
            self.build_buttons()

            embed = build_wache_embed(today, interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                f"🔴 Du wurdest aus **{slot_label(slot)}** ausgetragen.", ephemeral=True
            )
            return

        if len(liste) >= MAX_PLAETZE_PRO_SLOT:
            await interaction.response.send_message(
                "❌ Dieser Zeitraum ist leider gerade eben voll geworden. Bitte wähle einen anderen.",
                ephemeral=True
            )
            self.build_buttons()
            try:
                await interaction.message.edit(embed=build_wache_embed(today, interaction.guild), view=self)
            except Exception:
                pass
            return

        liste.append(uid)
        save_data(data)
        self.build_buttons()

        embed = build_wache_embed(today, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"🟢 Du bist eingetragen für **{slot_label(slot)}**!", ephemeral=True)


wache_view: "WacheView | None" = None

async def refresh_wache_nachricht(guild: discord.Guild):
    """Editiert (oder postet erstmalig) die EINE Buttons-Nachricht im
    Routenwache-Channel. Läuft live – bei jedem Ein-/Austragen."""
    if not data.get("channel_stempel"):
        return
    kanal = guild.get_channel(int(data["channel_stempel"]))
    if not kanal:
        return

    today = heute_key()
    wache_view.build_buttons()
    embed = build_wache_embed(today, guild)

    msg_id = data.get("stempel_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=wache_view)
            return
        except Exception as e:
            print(f"Alte Routenwache-Nachricht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed, view=wache_view)
    data["stempel_nachricht_id"] = str(msg.id)
    save_data(data)


async def geist_ping_neues_datum(guild: discord.Guild):
    """Pingt die Routenwache-Rolle EINMAL im Buttons-Channel und löscht die
    Ping-Nachricht sofort wieder (klassischer 'Geist-Ping'): Die Mitglieder
    bekommen die Erwähnungs-Benachrichtigung, aber im Channel bleibt nichts
    stehen. Wird bewusst NUR aufgerufen, wenn sich das Datum in der
    Routenwache-Nachricht wirklich ändert (Tageswechsel um 00:01 Uhr) –
    NICHT bei jedem normalen Ein-/Austragen-Update."""
    if not data.get("channel_stempel"):
        return
    kanal = guild.get_channel(int(data["channel_stempel"]))
    if not kanal:
        return

    rolle = guild.get_role(ROUTENWACHE_ROLLE_ID)
    mention_text = rolle.mention if rolle else f"<@&{ROUTENWACHE_ROLLE_ID}>"

    try:
        ping_msg = await kanal.send(
            mention_text,
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False)
        )
        await ping_msg.delete()
    except Exception as e:
        print(f"❌ Fehler beim Geist-Ping: {e}")


# ════════════════════════════════════════════════════════════════════════════
# 📋  TAGES-LOG (ein neuer Post pro abgeschlossenem Tag, um 00:01 Uhr)
# ════════════════════════════════════════════════════════════════════════════

def build_tages_log_embed(datum: str, guild: discord.Guild) -> discord.Embed:
    """Baut den Log-Embed für einen ABGESCHLOSSENEN Tag – wer war wann
    (welcher Zeitraum) eingetragen. Kompakte nummerierte Liste, analog zur
    Gesamtübersicht, hier aber 'wer war wann eingetragen' statt Stunden."""
    eintrag = data.get("tage", {}).get(datum, {})

    zeilen = []
    for slot in SLOTS:
        for uid in eintrag.get(slot, []):
            member = guild.get_member(int(uid)) if guild else None
            name = member.mention if member else f"Unbekanntes Mitglied ({uid})"
            zeilen.append(f"{name} — **{slot_label(slot)}**")

    if zeilen:
        beschreibung = "\n".join(f"{i}. {zeile}" for i, zeile in enumerate(zeilen, start=1))
    else:
        beschreibung = "Niemand war an diesem Tag für die Routenwache eingetragen."

    embed = discord.Embed(
        title=f"📋 Routenwache-Log ({datum})",
        description=beschreibung,
        color=EMBED_COLOR
    )
    embed.set_footer(text="ECLIPSE – Routenwache-Log • wer wann eingetragen war")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def poste_tages_log(guild: discord.Guild, datum: str):
    """Postet den Log-Eintrag für `datum` als NEUE Nachricht in den
    Log-Channel (kein Editieren – jeder Tag bekommt seinen eigenen Post,
    sodass eine durchsuchbare Historie entsteht)."""
    if not data.get("channel_stempel_liste"):
        return
    kanal = guild.get_channel(int(data["channel_stempel_liste"]))
    if not kanal:
        return
    embed = build_tages_log_embed(datum, guild)
    await kanal.send(embed=embed)


# ════════════════════════════════════════════════════════════════════════════
# 📊  GESAMTÜBERSICHT / LEADERBOARD (nur abgeschlossene Tage, 1x täglich)
# ════════════════════════════════════════════════════════════════════════════

def build_gesamtuebersicht_embed(guild: discord.Guild) -> discord.Embed:
    """Baut das Ranking 'wer hat insgesamt wie viele Stunden Routenwache
    gemacht' – wird sowohl vom /wache_gesamtuebersicht-Befehl als auch
    für den Leaderboard-Channel verwendet.

    Zeigt ALLE Mitglieder mit der Routenwache-Rolle (ROUTENWACHE_ROLLE_ID),
    auch wenn sie noch 0 Stunden haben – nicht nur die, die schon mal
    eingetragen waren. Wer 0 Stunden hat oder im Vergleich zu den anderen
    Rollenmitgliedern relativ wenig (unter der Hälfte des Durchschnitts),
    wird markiert, damit auf einen Blick sichtbar ist, wer noch dran
    (bald) sollte."""
    zaehler = gesamt_zeit_pro_user()

    rolle = guild.get_role(ROUTENWACHE_ROLLE_ID) if guild else None
    mitglieder = rolle.members if rolle else []

    if not rolle:
        beschreibung = "Die Routenwache-Rolle wurde auf dem Server nicht gefunden."
    elif not mitglieder:
        beschreibung = "Niemand hat aktuell die Routenwache-Rolle."
    else:
        eintraege = {str(m.id): zaehler.get(str(m.id), 0) for m in mitglieder}
        durchschnitt = sum(eintraege.values()) / len(eintraege)
        schwelle = durchschnitt / 2  # unter der Hälfte des Durchschnitts = "bald dran"

        sortiert = sorted(eintraege.items(), key=lambda x: x[1], reverse=True)
        zeilen = []
        for i, (uid, stunden) in enumerate(sortiert, start=1):
            member = guild.get_member(int(uid))
            name = member.mention if member else f"Unbekanntes Mitglied ({uid})"

            if stunden == 0:
                marker = "   ⚠️"
            elif stunden < schwelle:
                marker = "   ℹ️"
            else:
                marker = ""

            zeilen.append(f"**{i}.** {name} — **{stunden}h**{marker}")

        # Discord-Embed-Description ist auf 4096 Zeichen begrenzt
        beschreibung = "\n".join(zeilen)
        if len(beschreibung) > 4000:
            beschreibung = beschreibung[:4000] + "\n… (gekürzt)"

    embed = discord.Embed(
        title="📊 Gesamtübersicht Routenwache",
        description=beschreibung,
        color=EMBED_COLOR
    )
    embed.set_footer(text="ECLIPSE – Routenwache • Summe aller abgeschlossenen Tage • ⚠️ → keine Wache durchgeführt, ℹ️ → unter Durchschnitt • täglich 00:01 Uhr aktualisiert")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def refresh_gesamtuebersicht(guild: discord.Guild):
    """Editiert (oder postet erstmalig) die EINE Leaderboard-Nachricht im
    dafür gesetzten Channel. Wird bewusst NUR bei der täglichen
    00:01-Routine (und beim Bot-Start / Channel-Setup) aufgerufen – NICHT
    bei jedem Ein-/Austragen, da die Stunden erst zählen, wenn ein Tag
    abgeschlossen ist (siehe gesamt_zeit_pro_user)."""
    if not data.get("channel_gesamtuebersicht"):
        return
    kanal = guild.get_channel(int(data["channel_gesamtuebersicht"]))
    if not kanal:
        return

    embed = build_gesamtuebersicht_embed(guild)

    msg_id = data.get("gesamtuebersicht_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
            return
        except Exception as e:
            print(f"Alte Gesamtübersicht-Nachricht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed)
    data["gesamtuebersicht_nachricht_id"] = str(msg.id)
    save_data(data)


# ════════════════════════════════════════════════════════════════════════════
# 🎛️  SLASH-COMMANDS
# ════════════════════════════════════════════════════════════════════════════
# Admin/Leitung: /wache_channel_setzen, /wache_liste_channel_setzen,
# /set_leaderboard, /wache_posten, /wache_nachtragen, /channels.
# Für ALLE offen: /wache_eintragen, /wache_austragen, /meine_wache,
# /wache_gesamtuebersicht.

WACHE_CHOICES = [app_commands.Choice(name=slot_label(s), value=s) for s in SLOTS]


@tree.command(name="wache_channel_setzen", description="Setzt den Channel für die Routenwache-Buttons")
@app_commands.describe(channel="Der Channel wo die Zeitraum-Buttons gepostet werden")
@app_commands.check(ist_admin_oder_leitung)
async def wache_channel_setzen(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_stempel"] = channel.id
    data["stempel_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Routenwache-Channel gesetzt: {channel.mention}", ephemeral=True)
    await refresh_wache_nachricht(interaction.guild)


@tree.command(name="wache_liste_channel_setzen", description="Setzt den Channel für das tägliche Routenwache-Log (Post um 00:01 Uhr)")
@app_commands.describe(channel="Der Channel wo täglich um 00:01 Uhr der Log-Post landet")
@app_commands.check(ist_admin_oder_leitung)
async def wache_liste_channel_setzen(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_stempel_liste"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Routenwache-Log-Channel gesetzt: {channel.mention}\n"
        f"Dort erscheint ab jetzt jeden Tag um 00:01 Uhr automatisch ein neuer Log-Post mit den Daten des Vortages.",
        ephemeral=True
    )


@tree.command(name="set_leaderboard", description="Setzt den Channel für die Gesamtübersicht (täglich um 00:01 Uhr aktualisiert)")
@app_commands.describe(channel="Der Channel, in dem das Leaderboard täglich um 00:01 Uhr aktualisiert wird")
@app_commands.check(ist_admin_oder_leitung)
async def set_leaderboard(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_gesamtuebersicht"] = channel.id
    data["gesamtuebersicht_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(
        f"✅ Leaderboard-Channel gesetzt: {channel.mention}\n"
        f"Dort erscheint jetzt eine Nachricht mit dem Ranking, die täglich um 00:01 Uhr aktualisiert wird "
        f"(zählt nur bereits abgeschlossene Tage, der heutige Tag läuft ja noch).",
        ephemeral=True
    )
    await refresh_gesamtuebersicht(interaction.guild)


@tree.command(name="wache_posten", description="Postet oder aktualisiert die Routenwache-Nachricht (Zeitraum-Buttons)")
@app_commands.check(ist_admin_oder_leitung)
async def wache_posten(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await refresh_wache_nachricht(interaction.guild)
    await interaction.followup.send("✅ Routenwache-Nachricht gepostet/aktualisiert.", ephemeral=True)


@tree.command(name="wache_eintragen", description="Trägt ein Mitglied manuell in einen heutigen Zeitraum ein")
@app_commands.describe(mitglied="Das Mitglied", zeitraum="Der Zeitraum")
@app_commands.choices(zeitraum=WACHE_CHOICES)
async def wache_eintragen(interaction: discord.Interaction, mitglied: discord.Member, zeitraum: app_commands.Choice[str]):
    today = heute_key()
    eintrag = get_tag_eintrag(today)
    uid = str(mitglied.id)
    slot = zeitraum.value
    liste = eintrag.setdefault(slot, [])

    if uid in liste:
        await interaction.response.send_message(
            f"❌ {mitglied.mention} ist bereits für **{slot_label(slot)}** eingetragen.",
            ephemeral=True
        )
        return

    if len(liste) >= MAX_PLAETZE_PRO_SLOT:
        await interaction.response.send_message(f"❌ **{slot_label(slot)}** ist bereits voll.", ephemeral=True)
        return

    liste.append(uid)
    save_data(data)

    await interaction.response.send_message(f"✅ {mitglied.mention} wurde für **{slot_label(slot)}** eingetragen.", ephemeral=True)
    await refresh_wache_nachricht(interaction.guild)


@tree.command(name="wache_nachtragen", description="Trägt ein Mitglied nachträglich für einen VERGANGENEN Tag/Zeitraum ein")
@app_commands.describe(
    mitglied="Das Mitglied",
    datum="Datum im Format TT.MM.JJJJ (z.B. 27.07.2026)",
    zeitraum="Der Zeitraum"
)
@app_commands.choices(zeitraum=WACHE_CHOICES)
@app_commands.check(ist_admin_oder_leitung)
async def wache_nachtragen(interaction: discord.Interaction, mitglied: discord.Member, datum: str, zeitraum: app_commands.Choice[str]):
    try:
        tag = parse_datum(datum)
    except ValueError:
        await interaction.response.send_message(
            "❌ Ungültiges Datum. Bitte im Format **TT.MM.JJJJ** angeben (z.B. `27.07.2026`).", ephemeral=True
        )
        return

    eintrag = get_tag_eintrag(tag)
    uid = str(mitglied.id)
    slot = zeitraum.value
    liste = eintrag.setdefault(slot, [])

    if uid in liste:
        await interaction.response.send_message(
            f"❌ {mitglied.mention} ist für **{tag} — {slot_label(slot)}** bereits eingetragen.", ephemeral=True
        )
        return

    if len(liste) >= MAX_PLAETZE_PRO_SLOT:
        await interaction.response.send_message(
            f"❌ **{tag} — {slot_label(slot)}** ist bereits voll ({MAX_PLAETZE_PRO_SLOT}/{MAX_PLAETZE_PRO_SLOT}).", ephemeral=True
        )
        return

    liste.append(uid)
    save_data(data)

    await interaction.response.send_message(
        f"✅ {mitglied.mention} wurde nachträglich für **{tag} — {slot_label(slot)}** eingetragen.", ephemeral=True
    )

    # Wenn es sich um den heutigen Tag handelt, auch die Live-Buttons-Nachricht aktualisieren.
    # Die Gesamtübersicht wird bewusst NICHT sofort aktualisiert – sie zählt
    # erst wieder bei der täglichen 00:01-Routine (nur abgeschlossene Tage).
    if tag == heute_key():
        await refresh_wache_nachricht(interaction.guild)


@tree.command(name="wache_austragen", description="Trägt dich (oder ein anderes Mitglied) aus einem oder allen Zeiträumen aus")
@app_commands.describe(
    mitglied="Optional: anderes Mitglied austragen (Standard: du selbst)",
    zeitraum="Optional: nur aus diesem Zeitraum austragen (Standard: aus allen Zeiträumen des Tages)",
    datum="Optional: Datum im Format TT.MM.JJJJ, um einen vergangenen Tag zu korrigieren (Standard: heute)"
)
@app_commands.choices(zeitraum=WACHE_CHOICES)
async def wache_austragen(interaction: discord.Interaction, mitglied: discord.Member = None, zeitraum: app_commands.Choice[str] = None, datum: str = None):
    ziel = mitglied or interaction.user

    if datum:
        try:
            tag = parse_datum(datum)
        except ValueError:
            await interaction.response.send_message(
                "❌ Ungültiges Datum. Bitte im Format **TT.MM.JJJJ** angeben (z.B. `27.07.2026`).", ephemeral=True
            )
            return
    else:
        tag = heute_key()

    eintrag = get_tag_eintrag(tag)
    uid = str(ziel.id)

    if zeitraum:
        zu_entfernen = [zeitraum.value] if uid in eintrag.get(zeitraum.value, []) else []
    else:
        zu_entfernen = alle_slots_von_user(eintrag, uid)

    if not zu_entfernen:
        # BUGFIX: Hier fehlte zuvor die Verneinung ("nicht") – die Meldung
        # behauptete fälschlicherweise, die Person SEI eingetragen, obwohl
        # dieser Zweig genau dann läuft, wenn sie es NICHT ist.
        bezug = f"für **{slot_label(zeitraum.value)}**" if zeitraum else "für keinen Zeitraum"
        await interaction.response.send_message(f"❌ {ziel.mention} ist am **{tag}** nicht {bezug} eingetragen.", ephemeral=True)
        return

    for slot in zu_entfernen:
        eintrag[slot].remove(uid)
    save_data(data)

    zeitraeume_text = ", ".join(f"**{slot_label(s)}**" for s in zu_entfernen)
    await interaction.response.send_message(f"✅ {ziel.mention} wurde am **{tag}** aus {zeitraeume_text} ausgetragen.", ephemeral=True)

    if tag == heute_key():
        await refresh_wache_nachricht(interaction.guild)


@tree.command(name="meine_wache", description="Zeigt deinen heutigen Routenwache-Status")
async def meine_wache(interaction: discord.Interaction):
    today = heute_key()
    eintrag = get_tag_eintrag(today)
    uid = str(interaction.user.id)
    slots = alle_slots_von_user(eintrag, uid)

    if slots:
        text = "\n".join(f"🟢 **{slot_label(s)}**" for s in slots)
    else:
        text = "🔴 Du bist heute für keinen Zeitraum eingetragen."

    await interaction.response.send_message(f"**Deine Routenwache ({today})**\n{text}", ephemeral=True)


@tree.command(name="wache_gesamtuebersicht", description="Zeigt, wer insgesamt wie viele Stunden Routenwache gemacht hat")
async def wache_gesamtuebersicht(interaction: discord.Interaction):
    embed = build_gesamtuebersicht_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)


@tree.command(name="channels", description="Zeigt die aktuell gesetzten Channels für die Routenwache")
@app_commands.check(ist_admin_oder_leitung)
async def channels_info(interaction: discord.Interaction):
    stempel_ch = interaction.guild.get_channel(int(data["channel_stempel"])) if data.get("channel_stempel") else None
    stempel_liste_ch = interaction.guild.get_channel(int(data["channel_stempel_liste"])) if data.get("channel_stempel_liste") else None
    gesamt_ch = interaction.guild.get_channel(int(data["channel_gesamtuebersicht"])) if data.get("channel_gesamtuebersicht") else None

    await interaction.response.send_message(
        f"**Aktuelle Einstellungen – Routenwache:**\n\n"
        f"Routenwache (Buttons):  {stempel_ch.mention if stempel_ch else '❌ Nicht gesetzt – /wache_channel_setzen benutzen'}\n"
        f"Leaderboard (täglich 00:01 Uhr, nur abgeschlossene Tage):  {gesamt_ch.mention if gesamt_ch else '❌ Nicht gesetzt – /set_leaderboard benutzen'}\n"
        f"Routenwache-Log (täglich 00:01 Uhr):  {stempel_liste_ch.mention if stempel_liste_ch else '❌ Nicht gesetzt – /wache_liste_channel_setzen benutzen'}",
        ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════════════
# 🌙  TAGESWECHSEL (automatisch täglich um 00:01 Uhr)
# ════════════════════════════════════════════════════════════════════════════
letzter_bekannter_tag = None

@tasks.loop(time=dt_time(hour=0, minute=1, tzinfo=TIMEZONE))
async def tageswechsel_check():
    """Läuft jeden Tag exakt um 00:01 Uhr (Europe/Berlin):
    1. Postet den Log-Eintrag für den GESTRIGEN Tag in den Log-Channel
       (wer war wann eingetragen) – als neue Nachricht, damit im Channel
       eine durchsuchbare Historie entsteht.
    2. Setzt die Routenwache-Buttons-Nachricht für den neuen Tag auf.
    3. Aktualisiert die Leaderboard-Nachricht (zählt jetzt den gestrigen
       Tag als abgeschlossen mit).
    4. Geist-pingt die Routenwache-Rolle EINMAL im Buttons-Channel – NUR
       hier, weil sich hier wirklich das DATUM in der Nachricht ändert."""
    global letzter_bekannter_tag
    vorheriger_tag = letzter_bekannter_tag
    heute = heute_key()
    letzter_bekannter_tag = heute

    for guild in bot.guilds:
        try:
            if vorheriger_tag and vorheriger_tag != heute:
                await poste_tages_log(guild, vorheriger_tag)
            await refresh_wache_nachricht(guild)
            await refresh_gesamtuebersicht(guild)
            if vorheriger_tag and vorheriger_tag != heute:
                await geist_ping_neues_datum(guild)
            print(f"🌙 00:01 Tageswechsel: Log für {vorheriger_tag} gepostet, Routenwache für {heute} neu aufgesetzt.")
        except Exception as e:
            print(f"❌ Fehler beim Tageswechsel: {e}")

@tageswechsel_check.before_loop
async def before_tageswechsel_check():
    await bot.wait_until_ready()


# ════════════════════════════════════════════════════════════════════════════
# 🎯  BOT EVENTS
# ════════════════════════════════════════════════════════════════════════════

async def sync_commands():
    """Synct die Slash-Commands.

    KRITISCHER BUGFIX: Vorher wurde ZUERST `tree.clear_commands(guild=None)`
    aufgerufen. Das leert aber nicht nur die Commands auf Discords Seite,
    sondern auch die lokale Command-Liste im `tree`-Objekt selbst. Der
    darauffolgende Aufruf `tree.copy_global_to(guild=...)` kopiert die
    Commands aus genau dieser (jetzt leeren!) lokalen Liste in die Guild –
    das Ergebnis war, dass in der Guild am Ende GAR KEINE Commands mehr
    ankamen. Das erklärt "Bot ist online, aber kein Command eingebbar".

    Jetzt läuft zuerst der Guild-Sync (solange die lokale Command-Liste
    noch vollständig ist), und erst DANACH – und nur einmalig, dauerhaft
    in data.json vermerkt – die Bereinigung der alten globalen Reste."""
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            print(f"✅ {len(synced)} Commands sofort auf Guild {GUILD_ID} gesynct: {[c.name for c in synced]}")
        else:
            synced = await tree.sync()
            print(f"⚠️ Keine GUILD_ID gesetzt — {len(synced)} Commands global gesynct (kann bis zu 1h dauern).")
    except discord.HTTPException as e:
        print(f"❌ FEHLER beim Guild-Sync (evtl. Discord-Rate-Limit): {e}")
    except Exception as e:
        print(f"❌ FEHLER beim Guild-Sync: {e}")

    if not data.get("globale_befehle_bereinigt"):
        try:
            tree.clear_commands(guild=None)
            await tree.sync()
            data["globale_befehle_bereinigt"] = True
            save_data(data)
            print("🧹 Alte globale Befehle einmalig bereinigt.")
        except Exception as e:
            print(f"⚠️ Konnte globale Befehle nicht bereinigen (wird beim nächsten Start erneut versucht): {e}")


@bot.event
async def on_ready():
    global letzter_bekannter_tag, wache_view
    print(f"Bot online: {bot.user}")

    if wache_view is None:
        wache_view = WacheView()
    bot.add_view(wache_view)

    await sync_commands()

    letzter_bekannter_tag = heute_key()

    for guild in bot.guilds:
        try:
            await refresh_wache_nachricht(guild)
            await refresh_gesamtuebersicht(guild)
            print("✅ Routenwache-Nachricht & Leaderboard aufgesetzt.")
        except Exception as e:
            print(f"❌ Fehler beim Auto-Posten der Nachricht: {e}")

    if not tageswechsel_check.is_running():
        tageswechsel_check.start()

    print("Bot ist bereit!")


@bot.event
async def on_member_remove(member: discord.Member):
    """Entfernt automatisch die HEUTIGEN Routenwache-Einträge eines
    Mitglieds, sobald es den Server verlässt (Leave oder Kick).

    BUGFIX: Vorher wurden die Einträge über ALLE Tage entfernt – auch
    bereits abgeschlossene, die schon in die Gesamtübersicht (Leaderboard)
    eingeflossen waren. Verließ jemand den Server, verschwanden damit
    rückwirkend seine bereits "verdienten" Stunden aus der Statistik.
    Jetzt wird nur noch der laufende, heutige Tag bereinigt (macht Sinn,
    weil man ja nicht mehr da ist, um die Schicht anzutreten) –
    abgeschlossene Tage bleiben unangetastet."""
    uid = str(member.id)
    eintrag = data.get("tage", {}).get(heute_key(), {})
    geaendert = False

    for slot, liste in eintrag.items():
        if uid in liste:
            liste.remove(uid)
            geaendert = True

    if not geaendert:
        return

    save_data(data)
    print(f"🧹 Heutige Routenwache-Einträge von {member} ({uid}) entfernt (Server verlassen).")

    try:
        await refresh_wache_nachricht(member.guild)
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren nach Austritt: {e}")


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
