import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
import pytz
import json
import os

# ════════════════════════════════════════════════════════════════════════════
# ⚙️  KONFIGURATION
# ════════════════════════════════════════════════════════════════════════════
# Eigener Bot = eigenes Token und eigene GUILD_ID als Railway/Umgebungsvariable.
TOKEN    = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
TIMEZONE = pytz.timezone("Europe/Berlin")
EMBED_COLOR = 0xFFD700  # Gelb
DATA_DIR  = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Leitung: darf das Setup (Channel setzen, Nachricht posten) erledigen.
# Das eigentliche Ein-/Ausstempeln sowie das manuelle Nachtragen/Entfernen von
# Zeit ist bewusst für ALLE offen (keine Rollen-Einschränkung).
LEITUNG_ROLLE_ID = 1526202327483285629

def ist_admin_oder_leitung(interaction: discord.Interaction) -> bool:
    """True für echte Admins ODER Mitglieder mit der Leitungs-Rolle.
    Wird nur für die Setup-Befehle benutzt (Channel setzen / Nachricht posten)."""
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == LEITUNG_ROLLE_ID for r in interaction.user.roles)


# ════════════════════════════════════════════════════════════════════════════
# 💾  DATENSPEICHER
# ════════════════════════════════════════════════════════════════════════════
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            geladen = json.load(f)
    else:
        geladen = {}

    standard = {
        "channel_stempel": 1531376112226140260,
        "stempel_nachricht_id": None,
        "channel_stempel_liste": 1531376274130341909,
        "stempel_liste_nachricht_id": None,
        "stempel_nutzer": {},  # { "user_id": {"eingestempelt_seit": float|None, "gesamt_sekunden": float, "anzahl": int} }
    }
    if not geladen:
        return standard
    for key, wert in standard.items():
        geladen.setdefault(key, wert)
    return geladen

def save_data(data):
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
# 🛣️  ROUTENWACHE (Rein-/Raus-Tracking + Zeit-Übersicht)
# ════════════════════════════════════════════════════════════════════════════
# Komplett offen: es gibt hier absichtlich KEINE Rollen-/Berechtigungs-
# einschränkung. Jedes Mitglied kann sich ein-/ausstempeln UND Zeiten für
# sich selbst oder andere manuell nachtragen bzw. entfernen.

def get_stempel_eintrag(user_id: str):
    """Holt (oder erstellt) den Routenwache-Eintrag eines Nutzers."""
    nutzer = data.setdefault("stempel_nutzer", {})
    if user_id not in nutzer:
        nutzer[user_id] = {"eingestempelt_seit": None, "gesamt_sekunden": 0, "anzahl": 0}
    return nutzer[user_id]

def format_dauer(sekunden) -> str:
    """Formatiert Sekunden als 'Xd Yh Zm' bzw. 'Yh Zm' / 'Zm'."""
    sekunden = max(0, int(sekunden))
    tage, rest = divmod(sekunden, 86400)
    stunden, rest = divmod(rest, 3600)
    minuten = rest // 60

    teile = []
    if tage:
        teile.append(f"{tage}d")
    if stunden or tage:
        teile.append(f"{stunden}h")
    teile.append(f"{minuten}m")
    return " ".join(teile)

def build_stempel_embed():
    embed = discord.Embed(
        title="🛣️ Routenwache",
        description=(
            "Kurz und schmerzlos:\n"
            "🟢 **Rein** – du bist ab jetzt auf Route, die Zeit läuft.\n"
            "🔴 **Raus** – Feierabend, deine Zeit wird automatisch draufgerechnet.\n\n"
            f"Die Gesamtübersicht mit allen Zeiten gibt's in <#{data.get('channel_stempel_liste')}>.\n"
            "Und nicht vergessen wieder auszuchecken, sonst tickt die Uhr für immer weiter 😅"
        ),
        color=EMBED_COLOR
    )
    embed.set_footer(text="ECLIPSE – Routenwache")
    return embed

class StempelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="REIN", style=discord.ButtonStyle.success, custom_id="btn_stempel_ein")
    async def btn_ein(self, interaction: discord.Interaction, button: discord.ui.Button):
        eintrag = get_stempel_eintrag(str(interaction.user.id))
        if eintrag["eingestempelt_seit"] is not None:
            await interaction.response.send_message("❌ Du bist schon auf Route.", ephemeral=True)
            return
        eintrag["eingestempelt_seit"] = datetime.now(TIMEZONE).timestamp()
        save_data(data)
        await interaction.response.send_message("🟢 Bist drin. Viel Erfolg da draußen!", ephemeral=True)

    @discord.ui.button(label="RAUS", style=discord.ButtonStyle.danger, custom_id="btn_stempel_aus")
    async def btn_aus(self, interaction: discord.Interaction, button: discord.ui.Button):
        eintrag = get_stempel_eintrag(str(interaction.user.id))
        if eintrag["eingestempelt_seit"] is None:
            await interaction.response.send_message("❌ Du bist gerade gar nicht auf Route.", ephemeral=True)
            return

        dauer_sekunden = datetime.now(TIMEZONE).timestamp() - eintrag["eingestempelt_seit"]
        eintrag["gesamt_sekunden"] += dauer_sekunden
        eintrag["anzahl"] += 1
        eintrag["eingestempelt_seit"] = None
        save_data(data)

        await interaction.response.send_message(
            f"🔴 Feierabend! Diese Runde: **{format_dauer(dauer_sekunden)}**\n"
            f"Deine Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**",
            ephemeral=True
        )
        await update_stempel_liste(interaction.guild)

async def stempel_posten_intern(guild):
    if not data.get("channel_stempel"):
        return
    kanal = guild.get_channel(int(data["channel_stempel"]))
    if not kanal:
        return

    embed = build_stempel_embed()
    view  = StempelView()

    msg_id = data.get("stempel_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            print(f"Alte Routenwache-Nachricht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed, view=view)
    data["stempel_nachricht_id"] = str(msg.id)
    save_data(data)

def build_stempel_liste_embed(guild):
    embed = discord.Embed(title="📊 Routenwache – Übersicht", color=EMBED_COLOR)

    eintraege = [
        (uid, info) for uid, info in data.get("stempel_nutzer", {}).items()
        if info.get("gesamt_sekunden", 0) > 0 or info.get("anzahl", 0) > 0
    ]
    eintraege.sort(key=lambda x: x[1]["gesamt_sekunden"], reverse=True)

    if not eintraege:
        embed.description = "*Noch keine Zeiten erfasst.*"
        embed.set_footer(text="ECLIPSE – Routenwache")
        embed.timestamp = datetime.now(TIMEZONE)
        return embed

    zeilen = []
    for platz, (uid, info) in enumerate(eintraege, start=1):
        member = guild.get_member(int(uid))
        name = member.mention if member else f"Unbekanntes Mitglied ({uid})"
        zeilen.append(
            f"{platz}. {name} ({format_dauer(info['gesamt_sekunden'])} – {info['anzahl']} Zeiträume)"
        )

    beschreibung = "\n".join(zeilen)
    if len(beschreibung) > 4000:
        beschreibung = beschreibung[:4000] + "\n…"
    embed.description = beschreibung
    embed.set_footer(text="ECLIPSE – Routenwache")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def update_stempel_liste(guild):
    if not data.get("channel_stempel_liste"):
        return
    kanal = guild.get_channel(int(data["channel_stempel_liste"]))
    if not kanal:
        return

    embed = build_stempel_liste_embed(guild)
    msg_id = data.get("stempel_liste_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
            return
        except Exception as e:
            print(f"Alte Routenwache-Übersicht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed)
    data["stempel_liste_nachricht_id"] = str(msg.id)
    save_data(data)


# ─── Slash-Commands ────────────────────────────────────────────────────────────
# Hinweis: /stempel_posten, /set_stempel_liste und /zeitraum_entfernen bleiben
# Admin/Leitungs-Befehle (Setup bzw. destruktive Aktion). /zeit_hinzufuegen,
# /zeit_entfernen und /meine_zeit haben KEINE Berechtigungs-Einschränkung –
# jedes Mitglied kann sie nutzen.

@tree.command(name="set_stempel", description="Setzt den Channel für die Routenwache-Nachricht (Rein/Raus-Buttons)")
@app_commands.describe(channel="Der Channel wo die Rein/Raus-Buttons gepostet werden")
@app_commands.check(ist_admin_oder_leitung)
async def set_stempel(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_stempel"] = channel.id
    data["stempel_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Routenwache-Channel gesetzt: {channel.mention}", ephemeral=True)
    await stempel_posten_intern(interaction.guild)

@tree.command(name="set_stempel_liste", description="Setzt den Channel für die Routenwache-Übersicht")
@app_commands.describe(channel="Der Channel wo die Zeit-Übersicht aller Mitglieder gepostet wird")
@app_commands.check(ist_admin_oder_leitung)
async def set_stempel_liste(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_stempel_liste"] = channel.id
    data["stempel_liste_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Routenwache-Übersicht-Channel gesetzt: {channel.mention}", ephemeral=True)
    await update_stempel_liste(interaction.guild)

@tree.command(name="stempel_posten", description="Postet oder aktualisiert die Routenwache-Nachricht (Rein/Raus-Buttons)")
@app_commands.check(ist_admin_oder_leitung)
async def stempel_posten(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await stempel_posten_intern(interaction.guild)
    await update_stempel_liste(interaction.guild)
    await interaction.followup.send("✅ Routenwache-Nachricht gepostet/aktualisiert.", ephemeral=True)

@tree.command(name="zeit_hinzufuegen", description="Trägt manuell Zeit für ein Mitglied nach")
@app_commands.describe(
    mitglied="Das Mitglied, dem Zeit gutgeschrieben werden soll",
    stunden="Anzahl Stunden (optional)",
    minuten="Anzahl Minuten (optional)",
    datum="Datum, für das die Zeit gilt, z.B. 27.07.2026 (nur zur Dokumentation)"
)
async def zeit_hinzufuegen(interaction: discord.Interaction, mitglied: discord.Member, stunden: int = 0, minuten: int = 0, datum: str = None):
    if stunden <= 0 and minuten <= 0:
        await interaction.response.send_message("❌ Bitte Stunden und/oder Minuten angeben.", ephemeral=True)
        return
    if stunden < 0 or minuten < 0:
        await interaction.response.send_message("❌ Stunden/Minuten dürfen nicht negativ sein.", ephemeral=True)
        return

    sekunden = stunden * 3600 + minuten * 60
    eintrag = get_stempel_eintrag(str(mitglied.id))
    eintrag["gesamt_sekunden"] += sekunden
    eintrag["anzahl"] += 1
    save_data(data)

    await update_stempel_liste(interaction.guild)

    datum_text = f" (Datum: {datum})" if datum else ""
    await interaction.response.send_message(
        f"✅ {mitglied.mention} wurden **{format_dauer(sekunden)}** gutgeschrieben{datum_text}.\n"
        f"Neue Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**",
        ephemeral=True
    )

@tree.command(name="zeit_entfernen", description="Zieht manuell Zeit von einem Mitglied ab")
@app_commands.describe(
    mitglied="Das Mitglied, dem Zeit abgezogen werden soll",
    stunden="Anzahl Stunden (optional)",
    minuten="Anzahl Minuten (optional)",
    datum="Datum, für das die Zeit gilt, z.B. 27.07.2026 (nur zur Dokumentation)"
)
async def zeit_entfernen(interaction: discord.Interaction, mitglied: discord.Member, stunden: int = 0, minuten: int = 0, datum: str = None):
    if stunden <= 0 and minuten <= 0:
        await interaction.response.send_message("❌ Bitte Stunden und/oder Minuten angeben.", ephemeral=True)
        return
    if stunden < 0 or minuten < 0:
        await interaction.response.send_message("❌ Stunden/Minuten dürfen nicht negativ sein.", ephemeral=True)
        return

    sekunden = stunden * 3600 + minuten * 60
    eintrag = get_stempel_eintrag(str(mitglied.id))
    eintrag["gesamt_sekunden"] = max(0, eintrag["gesamt_sekunden"] - sekunden)
    eintrag["anzahl"] = max(0, eintrag["anzahl"] - 1)
    save_data(data)

    await update_stempel_liste(interaction.guild)

    datum_text = f" (Datum: {datum})" if datum else ""
    await interaction.response.send_message(
        f"✅ {mitglied.mention} wurden **{format_dauer(sekunden)}** abgezogen{datum_text}.\n"
        f"Neue Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**",
        ephemeral=True
    )

@tree.command(name="zeitraum_entfernen", description="Löscht die komplette Routenwache-Statistik eines Mitglieds unwiederbringlich")
@app_commands.describe(mitglied="Das Mitglied, dessen komplette Routenwache-Statistik gelöscht werden soll")
@app_commands.check(ist_admin_oder_leitung)
async def zeitraum_entfernen(interaction: discord.Interaction, mitglied: discord.Member):
    uid = str(mitglied.id)
    nutzer = data.get("stempel_nutzer", {})

    if uid not in nutzer or (nutzer[uid]["gesamt_sekunden"] == 0 and nutzer[uid]["anzahl"] == 0):
        await interaction.response.send_message(f"❌ Für {mitglied.mention} sind keine Zeiten erfasst.", ephemeral=True)
        return

    nutzer[uid] = {"eingestempelt_seit": None, "gesamt_sekunden": 0, "anzahl": 0}
    save_data(data)

    await update_stempel_liste(interaction.guild)

    await interaction.response.send_message(
        f"🗑️ Die komplette Routenwache-Statistik von {mitglied.mention} wurde gelöscht.",
        ephemeral=True
    )

@tree.command(name="meine_zeit", description="Zeigt deinen eigenen Routenwache-Status")
async def meine_zeit(interaction: discord.Interaction):
    eintrag = get_stempel_eintrag(str(interaction.user.id))
    save_data(data)
    status_text = "🟢 gerade auf Route" if eintrag["eingestempelt_seit"] else "🔴 gerade nicht auf Route"
    await interaction.response.send_message(
        f"**Deine Routenwache**\n"
        f"Status: {status_text}\n"
        f"Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**\n"
        f"Zeiträume: **{eintrag['anzahl']}**",
        ephemeral=True
    )

@tree.command(name="channels", description="Zeigt die aktuell gesetzten Channels für die Routenwache")
@app_commands.check(ist_admin_oder_leitung)
async def channels_info(interaction: discord.Interaction):
    stempel_ch = interaction.guild.get_channel(int(data["channel_stempel"])) if data.get("channel_stempel") else None
    stempel_liste_ch = interaction.guild.get_channel(int(data["channel_stempel_liste"])) if data.get("channel_stempel_liste") else None

    await interaction.response.send_message(
        f"**Aktuelle Einstellungen – Routenwache:**\n\n"
        f"Routenwache:           {stempel_ch.mention        if stempel_ch        else '❌ Nicht gesetzt – /set_stempel benutzen'}\n"
        f"Routenwache-Übersicht: {stempel_liste_ch.mention  if stempel_liste_ch  else '❌ Nicht gesetzt – /set_stempel_liste benutzen'}",
        ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════════════
# 🎯  BOT EVENTS
# ════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            print(f"✅ {len(synced)} Commands SOFORT auf Guild {GUILD_ID} gesynct: {[c.name for c in synced]}")
        else:
            print("⚠️ Keine GUILD_ID gesetzt — sync läuft global (kann bis zu 1h dauern).")

        synced_global = await tree.sync()
        print(f"✅ {len(synced_global)} Commands global gesynct: {[c.name for c in synced_global]}")
    except Exception as e:
        print(f"❌ FEHLER beim Sync: {e}")

    bot.add_view(StempelView())

    for guild in bot.guilds:
        try:
            if data.get("channel_stempel") and not data.get("stempel_nachricht_id"):
                await stempel_posten_intern(guild)
                print("✅ Routenwache-Nachricht nachträglich gepostet.")
            if data.get("channel_stempel_liste"):
                await update_stempel_liste(guild)
                print("✅ Routenwache-Übersicht nachträglich gepostet/aktualisiert.")
        except Exception as e:
            print(f"❌ Fehler beim Auto-Posten fehlender Nachrichten: {e}")

    print("Bot ist bereit!")

@bot.event
async def on_member_remove(member: discord.Member):
    """Entfernt automatisch den Routenwache-Eintrag eines Mitglieds,
    sobald es den Server verlässt (Leave oder Kick)."""
    uid = str(member.id)
    nutzer = data.get("stempel_nutzer", {})

    if uid not in nutzer:
        return

    del nutzer[uid]
    save_data(data)
    print(f"🧹 Routenwache-Eintrag von {member} ({uid}) entfernt (Server verlassen).")

    try:
        await update_stempel_liste(member.guild)
    except Exception as e:
        print(f"❌ Fehler beim Aktualisieren der Übersicht nach Austritt: {e}")

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
