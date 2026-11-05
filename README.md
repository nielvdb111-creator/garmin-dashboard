# Lijf: je Garmin-dashboard (gratis, privé, op je gsm)

## Wat het doet

GitHub haalt drie keer per dag je Garmin-data op via de **officiële Garmin Connect Personal API**. Het script versleutelt alles en zet het op een gratis GitHub Pages-site. Op je gsm open je die site en ontgrendel je ze met je wachtwoord.

```
Garmin-horloge → Garmin Connect → GitHub Actions (garmin_sync.py) → versleuteld → GitHub Pages → je gsm
```

- **Kosten: €0.** De Garmin Personal API, GitHub Actions en GitHub Pages zijn gratis voor publieke repo's. Er is geen Claude-licentie of betaald abonnement nodig.
- **Er draait niets op je pc.** Je laptop hoeft niet aan te staan en je werkbeveiliging komt er niet aan te pas.
- **Privé:** online staan alleen versleutelde bestanden (AES-256-GCM met PBKDF2, 600.000 rondes). De code is publiek, je data niet. De logboeken van de runs bevatten alleen aantallen, geen gezondheidswaarden.
- **Toekomstbestendig:** het dashboard is gewone HTML zonder server en zonder externe bibliotheken.

## Installatie (±15 min, alles in de browser)

### 1. Garmin-sleutel
1. Ga naar https://connect.garmin.com/app/settings/personalApiToken
2. Verwijder oude sleutels die je ergens geplakt hebt.
3. Klik op **Create Key**, kies **1 jaar** en kopieer de sleutel. Je ziet hem maar één keer; plak hem in stap 4.

### 2. Repo maken
1. Klik op github.com rechtsboven op **+** en dan op **New repository**.
2. Geef als naam `garmin-dashboard`, kies **Public** en vink **Add a README file** aan. Klik op **Create repository**.

### 3. Bestanden uploaden
1. Klik in de repo op **Add file** en dan op **Upload files**.
2. Sleep deze vier items uit de uitgepakte zip in het venster: `garmin_sync.py`, `requirements.txt`, `README.md` en de map `site`. Klik op **Commit changes**.
3. Het workflowbestand maak je apart aan, omdat de browser mappen met een punt soms overslaat:
   1. Klik op **Add file** en dan op **Create new file**.
   2. Typ als naam exact: `.github/workflows/sync.yml`. De slashes maken vanzelf mappen aan.
   3. Open `.github/workflows/sync.yml` uit de zip in Kladblok, kopieer alles en plak het in het venster.
   4. Klik op **Commit changes**.

### 4. Geheimen instellen
Ga in de repo naar **Settings → Secrets and variables → Actions → New repository secret** en maak er twee aan:

| Name | Secret |
|---|---|
| `GARMIN_PERSONAL_API_KEY` | je Garmin-sleutel uit stap 1 |
| `GARMIN_DASH_PASSPHRASE` | een zelfgekozen dashboardwachtwoord van minstens 16 tekens, bijvoorbeeld 4-5 losse woorden. Bewaar het in je wachtwoordmanager. |

Na het opslaan kan niemand de secrets nog lezen, ook jij niet.

### 5. Eerste run
1. Open het tabblad **Actions**. Krijg je de vraag om workflows aan te zetten, bevestig dan.
2. Klik links op **Garmin sync**, dan rechts op **Run workflow** en nog eens op **Run workflow**.
3. Na 1 à 3 minuten verschijnt er een groen vinkje. Bij een rood kruis klik je de run open en stuur je de rode tekst door.

### 6. Pages aanzetten (eenmalig, na de eerste groene run)
1. Ga naar **Settings → Pages** en kies **Deploy from a branch**.
2. Kies branch `gh-pages`, map `/ (root)` en klik op **Save**.
3. Wacht een paar minuten. Bovenaan verschijnt dan je link: `https://<jouw-naam>.github.io/garmin-dashboard/`.

### 7. Op je gsm
1. Open de link, typ je wachtwoord en vink **Onthoud op dit toestel** aan.
2. Zet de pagina op je beginscherm:
   - iPhone: Safari → Deel → **Zet op beginscherm**
   - Android: Chrome → ⋮ → **Toevoegen aan startscherm**

## Wat je ziet

| Blok | Inhoud |
|---|---|
| Jij vs. jouw normaal | Slaapscore, HRV, rusthartslag, Body Battery, stress en stappen, telkens vergeleken met de mediaan van je 30 dagen ervoor. Groen betekent beter, rood minder, grijs gelijk. |
| Herstel | Slaapfases per nacht, slaapscore, HRV met je evenwichtige zone, rusthartslag |
| Energie & stress | Body Battery (laagste tot hoogste stand per dag) en stress per dag |
| Training | Uren per week per sport, laatste activiteiten, belasting, VO2max |
| Dagelijks | Stappen tegenover je doel, intensiteitsminuten tegenover het WHO-advies van 150 per week |
| Weetjes | Fitnessleeftijd, stappenreeks, afstand uitgedrukt in "keer Antwerpen–Brussel", beste nacht, regelmaat van je bedtijd, HRV na een trainingsdag tegenover een rustdag, drukste dag, sport van het jaar, SpO₂, slaap per weekdag, sportverdeling en een jaarkalender van je activiteiten |

Met de periodeknop kies je 7 dagen, 30 dagen, 90 dagen of 1 jaar. Tik op een grafiek voor de details.

## Onderhoud

| Wanneer | Wat |
|---|---|
| **Elk jaar** | Je Garmin-sleutel verloopt. Maak een nieuwe aan en vervang het secret `GARMIN_PERSONAL_API_KEY` (Settings → Secrets → potloodje). Zet hiervoor een herinnering in je agenda. |
| Rode banner "Laatste sync is X dagen oud" | Kijk bij **Actions** welke run rood is en open die. |
| GitHub mailt "scheduled workflow disabled" | Dat gebeurt soms bij lange inactiviteit. Ga naar Actions → Garmin sync → **Enable workflow**. |
| Een blok blijft leeg | Draai Actions → Garmin sync → Run workflow met het vinkje **schema** aan. De log toont dan alleen de structuur van Garmins antwoorden, zonder waarden. Stuur die door, dan pas ik de koppeling aan. |
| Wachtwoord wijzigen | Pas het secret `GARMIN_DASH_PASSPHRASE` aan en start een run. Op je gsm vraagt het dashboard dan vanzelf het nieuwe wachtwoord. |
| Handmatig bijwerken | Actions → Garmin sync → Run workflow |
| Stoppen | Verwijder de repo en de sleutel in Garmin Connect. |

## Bestanden

| Bestand | Rol |
|---|---|
| `garmin_sync.py` | Ophalen, omzetten en versleutelen (`--ci` op GitHub, of lokaal op een eigen pc) |
| `requirements.txt` | De Python-pakketten: de officiële Garmin-client, cryptography en requests |
| `.github/workflows/sync.yml` | Het schema: 06:20, 12:20 en 21:20 Belgische wintertijd (in de zomer een uur later) |
| `site/` | Het dashboard. Wijzigingen gaan bij de volgende run vanzelf live. |
| `setup.ps1` | Alleen voor een eigen Windows-pc als alternatief. **Niet** gebruiken op een werklaptop. |

## Goed om te weten

- **Wat publiek is:** de code van het dashboard en de branch `gh-pages` met **versleutelde** bestanden. Elke run vervangt die branch volledig, dus oude versies blijven niet staan. De gegevens die het script tussen runs onthoudt, staan daar ook versleuteld.
- **Je secrets** zijn nergens zichtbaar, ook niet in de logs. Kopieën van je repo (forks) krijgen ze niet.
- **"Onthoud op dit toestel"** bewaart niet je wachtwoord. Het bewaart alleen een afgeleide sleutel die niet uit te lezen is.
- **Garmin-voorwaarden:** de Personal API is bedoeld voor je eigen data en je eigen gebruik. Dat is precies wat dit doet.
- Dit dashboard is **geen medisch advies**. "Normaal" is je eigen mediaan van de voorbije 30 dagen en geen gezondheidsnorm.
