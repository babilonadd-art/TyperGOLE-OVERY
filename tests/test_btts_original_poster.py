import io
import unittest
from pathlib import Path

from PIL import Image, ImageChops

import bot


class OriginalBttsPosterTests(unittest.TestCase):
    def test_double_uses_full_original_poster_and_only_fills_three_fields(self):
        original_path = Path("assets/btts_original_poster.png")
        self.assertTrue(original_path.is_file())

        pair = [
            {
                "home": "Arsenal",
                "away": "Chelsea",
                "league": "Premier League",
                "kickoff_time": "2026-11-27T18:00:00+01:00",
                "odds": "1.80",
            },
            {
                "home": "Real Madrid",
                "away": "Barcelona",
                "league": "La Liga",
                "kickoff_time": "2026-11-27T20:00:00+01:00",
                "odds": "1.90",
            },
        ]

        rendered = bot.render_btts_double_graphic(pair)
        output = Image.open(io.BytesIO(rendered.getvalue())).convert("RGB")
        with Image.open(original_path) as source_file:
            source = source_file.convert("RGB")

        self.assertEqual(source.size, (720, 1640))
        self.assertEqual(output.size, source.size)

        # Header, original bull artwork and bottom BTTS logo remain untouched.
        for region in ((0, 0, 720, 700), (0, 1130, 720, 1640)):
            self.assertIsNone(
                ImageChops.difference(source.crop(region), output.crop(region)).getbbox()
            )

        # Text is drawn inside the two match fields and the combined-odds field.
        for region in ((24, 716, 696, 846), (24, 852, 696, 982), (24, 988, 696, 1120)):
            self.assertIsNotNone(
                ImageChops.difference(source.crop(region), output.crop(region)).getbbox()
            )


if __name__ == "__main__":
    unittest.main()
