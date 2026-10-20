import tomllib
import unittest

from noha import diagnostics, settings
from noha.config import Config


class TomlWriterTests(unittest.TestCase):
    def test_replaces_values_and_keeps_comments_and_other_keys(self):
        text = '[voice]\nmodel = "gpt-realtime-1.5" # cheapest\nvoice = "marin"\n\n[motion]\ngpio = 3\n'
        out = settings.set_toml(text, "voice", "voice", "alloy")
        self.assertIn('voice = "alloy"', out)
        self.assertIn('model = "gpt-realtime-1.5" # cheapest', out)
        out = settings.set_toml(out, "voice", "model", "gpt-live-1")
        self.assertIn('model = "gpt-live-1" # cheapest', out)
        self.assertEqual(tomllib.loads(out)["motion"]["gpio"], 3)

    def test_multi_line_arrays_are_replaced_whole(self):
        text = '[greeting]\ntexts = [\n  "um", # primeiro\n  "dois",\n] # fim\nlocal = true\n[leds]\nenabled = true\n'
        out = settings.set_toml(text, "greeting", "texts", ["três"])
        self.assertEqual(tomllib.loads(out), {"greeting": {"texts": ["três"], "local": True}, "leds": {"enabled": True}})

    def test_missing_keys_and_sections_are_added(self):
        out = settings.set_toml('[motion]\ngpio = 3\n\n[leds]\n', "motion", "confirm_seconds", 6.0)
        self.assertEqual(tomllib.loads(out)["motion"], {"gpio": 3, "confirm_seconds": 6.0})
        out = settings.set_toml('[motion]\ngpio = 3', "greeting", "local", False)
        self.assertEqual(tomllib.loads(out)["greeting"], {"local": False})
        out = settings.set_toml('[voice]\n[motion]\ngpio = 3\n', "voice", "voice", "cedar")
        self.assertEqual(tomllib.loads(out)["voice"], {"voice": "cedar"})

    def test_commented_keys_are_not_mistaken_for_settings(self):
        text = '[greeting]\n# texts = ["exemplo"]\nlocal = true\n'
        out = settings.set_toml(text, "greeting", "texts", ["Oi"])
        self.assertIn('# texts = ["exemplo"]', out)
        self.assertEqual(tomllib.loads(out)["greeting"]["texts"], ["Oi"])

    def test_strings_with_quotes_and_accents_round_trip(self):
        value = 'Olá, "visitante"! \\ Tudo bem?'
        out = settings.apply("[greeting]\n", {"greeting.style": value})
        self.assertEqual(tomllib.loads(out)["greeting"]["style"], value)

    def test_template_accepts_every_setting(self):
        text = open("config/noha.toml", encoding="utf-8").read()
        config = Config.load("config/noha.toml")
        values = settings.values(config, "Personalidade")
        changes = {key: values[key] for key in values if settings.BY_KEY[key].section}
        self.assertEqual(tomllib.loads(settings.apply(text, changes)), tomllib.loads(settings.apply(text, changes)))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_values_are_normalized(self):
        result = settings.validate({"greeting.texts": [" Oi ", "", "Olá"], "voice.daily_budget_usd": 3,
                                    "greeting.style": "  calmo ", "openai_key": ""}, self.config)
        self.assertEqual(result, {"greeting.texts": ["Oi", "Olá"], "voice.daily_budget_usd": 3.0, "greeting.style": "calmo"})

    def test_errors_name_the_field(self):
        cases = {"motion.confirm_pulses": 0, "leds.enabled": "sim", "voice.eagerness": "turbo", "motion.gpio": 40,
                 "personality": "", "greeting.texts": [], "openai_key": "curta"}
        for key, value in cases.items():
            with self.assertRaisesRegex(ValueError, settings.BY_KEY[key].label.split(" ")[0], msg=key):
                settings.validate({key: value}, self.config)

    def test_voice_must_exist_for_the_resulting_model(self):
        with self.assertRaisesRegex(ValueError, "Voz incompatível"):
            settings.validate({"voice.model": "gpt-live-1"}, self.config)  # alloy is not a GPT-Live voice
        self.assertEqual(settings.validate({"voice.model": "gpt-live-1", "voice.voice": "marin"}, self.config)["voice.voice"], "marin")

    def test_schema_describes_every_group(self):
        schema = settings.schema()
        groups = {group["id"] for group in schema["groups"]}
        self.assertTrue(all(item["group"] in groups for item in schema["settings"]))
        self.assertEqual(len({item["key"] for item in schema["settings"]}), len(schema["settings"]))

    def test_config_load_reports_unknown_options(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as folder:
            path = Path(folder) / "noha.toml"
            path.write_text("[voice]\nbogus = 1\n")
            with self.assertRaisesRegex(ValueError, r"Opção desconhecida em \[voice\]"):
                Config.load(path)
            path.write_text("[leds]\nbrightness = 300\n")
            with self.assertRaisesRegex(ValueError, "Brilho"):
                Config.load(path)
            path.write_text("[voice]\nvoice = \"cedar\"\n")
            first = Config.load(path).digest
            path.write_text("[voice]\nvoice = \"marin\"\n")
            self.assertNotEqual(first, Config.load(path).digest)


class DiagnosticsTests(unittest.TestCase):
    def status(self, **overrides):
        status = {"voice": {"stale": False, "version": "2.0.0", "audio_ready": True, "gpio_ready": True, "gpio": 3,
                            "greeting_local": True, "local_greetings": 4, "setup_ready": True},
                  "network": {"mode": "online", "wifi": "Casa"}, "key_configured": True, "presence": True,
                  "budget": {"limit": 2.0, "spent": 0.5}, "security": {"default_password": False},
                  "updates": {"state": "current"}, "ngrok": {"state": "online"}}
        for key, value in overrides.items():
            status[key] = {**status[key], **value} if isinstance(value, dict) and isinstance(status.get(key), dict) else value
        return status

    def levels(self, status):
        return {item["id"]: item["level"] for item in diagnostics.checks(status)}

    def test_healthy_robot_is_all_ok(self):
        self.assertTrue(all(level == "ok" for level in self.levels(self.status()).values()))

    def test_problems_come_first_with_a_way_to_fix_them(self):
        items = diagnostics.checks(self.status(key_configured=False, voice={"audio_missing": ["Microfone"]},
                                               budget={"spent": 1.8}, security={"default_password": True}))
        self.assertEqual([i["level"] for i in items[:3]], ["error", "error", "error"])
        by_id = {item["id"]: item for item in items}
        self.assertEqual(by_id["chave"]["action"], "ajustes/chave")
        self.assertIn("Microfone", by_id["audio"]["detail"])
        self.assertEqual(by_id["custo"]["level"], "warn")
        self.assertEqual(by_id["senha"]["action"], "sistema/seguranca")

    def test_offline_robot_names_the_setup_network(self):
        items = {i["id"]: i for i in diagnostics.checks(self.status(network={"mode": "configuracao", "ssid": "Noha-Setup-AB12"}))}
        self.assertEqual(items["internet"]["level"], "error")
        self.assertIn("Noha-Setup-AB12", items["internet"]["detail"])

    def test_stale_voice_service_hides_hardware_guesses(self):
        levels = self.levels(self.status(voice={"stale": True}))
        self.assertEqual(levels["voz"], "error")
        self.assertNotIn("audio", levels)
        self.assertNotIn("sensor", levels)

    def test_budget_reached_and_greetings_missing(self):
        levels = self.levels(self.status(budget={"spent": 2.5}, voice={"local_greetings": 0}, presence=False))
        self.assertEqual((levels["custo"], levels["saudacoes"], levels["sensor"]), ("error", "warn", "warn"))


if __name__ == "__main__":
    unittest.main()
