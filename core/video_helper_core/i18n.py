"""Minimal UI translation: English source strings in code, German table below.

Widgets call tr() when they are built; switching the language rebuilds the tool pages.
"""

LANGUAGES = [("de", "Deutsch"), ("en", "English")]
DEFAULT_LANGUAGE = "de"

_current = DEFAULT_LANGUAGE


def set_language(code):
    global _current
    _current = code if code in dict(LANGUAGES) else DEFAULT_LANGUAGE


def current_language():
    return _current


def register_german(table):
    """Adds an app's German texts to the shared table."""
    GERMAN.update(table)


def tr(text, **kwargs):
    if _current == "de":
        text = GERMAN.get(text, text)
    return text.format(**kwargs) if kwargs else text


# Texts of the shared shell plus texts both apps use; each app adds its own with register_german().
GERMAN = {
    'Cancel': 'Abbrechen',
    'New version v{version} available': 'Neue Version v{version} verfügbar',
    'About': 'Über',
    'Choose a tool to start': 'Wähle ein Werkzeug',
    'Back': 'Zurück',
    'Back to the overview ({shortcut})': 'Zurück zur Übersicht ({shortcut})',
    'Language': 'Sprache',
    'Jobs still running': 'Vorgänge laufen noch',
    'A job is still running. Quit anyway?\nRunning compressions are aborted and their unfinished output files deleted.':
        'Es läuft noch ein Vorgang. Trotzdem beenden?\nLaufende Komprimierungen werden abgebrochen und ihre unfertigen Zieldateien gelöscht.',
    'Settings': 'Einstellungen',
    'Done': 'Fertig',
    'Stop': 'Stopp',
    'Success': 'Erfolg',
    'Error': 'Fehler',
    'Browse…': 'Durchsuchen…',
    'Target folder:': 'Zielordner:',
    'Select target folder': 'Zielordner auswählen',
    'Limits': 'Begrenzen',
    'Ready': 'Bereit',
    'Language:': 'Sprache:',
    'About {name}': 'Über {name}',
    'Please stop running jobs first, then change the language.':
        'Bitte beende zuerst laufende Aufträge und ändere dann die Sprache.',
    'Update check: switched off.': 'Update-Prüfung: ausgeschaltet.',
    'Update check failed: {error}': 'Update-Prüfung fehlgeschlagen: {error}',
    'Update check: version {version} is available.': 'Update-Prüfung: Version {version} ist verfügbar.',
    'Update check: this is the newest version (latest release: {version}).': 'Update-Prüfung: Das ist die neueste Version (letztes Release: {version}).',
    'Update check: still running…': 'Update-Prüfung: läuft noch…',
}
