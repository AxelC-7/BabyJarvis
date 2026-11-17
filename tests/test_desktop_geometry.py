import unittest

from desktop_geometry import ScreenGeometry, image_point_to_desktop


class ImagePointMappingTests(unittest.TestCase):
    def test_equal_size_frame_maps_pixels_without_offset_drift(self) -> None:
        screen = ScreenGeometry(0, 0, 1366, 768)

        self.assertEqual(
            image_point_to_desktop(1200, 640, 1366, 768, screen),
            (1200, 640),
        )

    def test_scaled_frame_maps_corners_and_center(self) -> None:
        screen = ScreenGeometry(0, 0, 1366, 768)

        self.assertEqual(
            image_point_to_desktop(0, 0, 1920, 1080, screen),
            (0, 0),
        )
        self.assertEqual(
            image_point_to_desktop(1919, 1079, 1920, 1080, screen),
            (1365, 767),
        )
        self.assertEqual(
            image_point_to_desktop(68, 974, 1920, 1080, screen),
            (48, 692),
        )

    def test_non_primary_origin_is_applied(self) -> None:
        screen = ScreenGeometry(-1600, -180, 3520, 1260)

        self.assertEqual(
            image_point_to_desktop(0, 0, 3520, 1260, screen),
            (-1600, -180),
        )
        self.assertEqual(
            image_point_to_desktop(3519, 1259, 3520, 1260, screen),
            (1919, 1079),
        )

    def test_multiple_capture_sizes_map_to_physical_bounds(self) -> None:
        cases = (
            (640, 360, 1920, 1080),
            (2560, 1440, 3840, 2160),
            (1280, 800, 1920, 1200),
        )
        for image_width, image_height, screen_width, screen_height in cases:
            with self.subTest(
                image=(image_width, image_height),
                screen=(screen_width, screen_height),
            ):
                screen = ScreenGeometry(0, 0, screen_width, screen_height)
                point = image_point_to_desktop(
                    image_width - 1,
                    image_height - 1,
                    image_width,
                    image_height,
                    screen,
                )
                self.assertEqual(point, (screen_width - 1, screen_height - 1))
                self.assertTrue(screen.contains(*point))

    def test_rejects_out_of_bounds_and_invalid_coordinates(self) -> None:
        screen = ScreenGeometry(0, 0, 1366, 768)

        for point in ((-1, 0), (1920, 0), (0, 1080)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                image_point_to_desktop(*point, 1920, 1080, screen)
        with self.assertRaises(TypeError):
            image_point_to_desktop(True, 0, 1920, 1080, screen)
        with self.assertRaises(ValueError):
            image_point_to_desktop(0, 0, 0, 1080, screen)
        with self.assertRaises(ValueError):
            image_point_to_desktop(0, 0, 1, 1, screen)

    def test_rejects_aspect_ratio_changes(self) -> None:
        screen = ScreenGeometry(0, 0, 1920, 1080)

        with self.assertRaisesRegex(ValueError, "distintas proporciones"):
            image_point_to_desktop(100, 100, 800, 600, screen)


if __name__ == "__main__":
    unittest.main()
