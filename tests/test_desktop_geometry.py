import unittest
from unittest.mock import Mock, patch

from desktop_geometry import (
    ScreenGeometry,
    get_primary_screen_geometry,
    image_point_to_desktop,
    rectangle_intersects_geometry,
)


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

    def test_primary_monitor_capture_geometry_excludes_secondary_monitor(self) -> None:
        primary = ScreenGeometry(0, 0, 1920, 1080)
        virtual_desktop = ScreenGeometry(-1280, 0, 3200, 1080)
        self.assertEqual(primary.width, 1920)
        self.assertEqual(primary.height, 1080)
        self.assertEqual(virtual_desktop.width, 3200)
        self.assertEqual(
            image_point_to_desktop(1919, 1079, 1920, 1080, primary),
            (1919, 1079),
        )
        self.assertFalse(rectangle_intersects_geometry(1920, 100, 2200, 600, primary))
        self.assertTrue(rectangle_intersects_geometry(1800, 100, 2000, 600, primary))

    def test_primary_monitor_geometry_uses_primary_system_metrics(self) -> None:
        user32 = Mock()
        user32.GetSystemMetrics.side_effect = lambda metric: {0: 1920, 1: 1080}[metric]
        with (
            patch("desktop_geometry.sys.platform", "win32"),
            patch("desktop_geometry.initialize_dpi_awareness", return_value=("per-monitor-v2", 96)),
            patch("desktop_geometry.ctypes.WinDLL", return_value=user32),
        ):
            geometry = get_primary_screen_geometry()
        self.assertEqual(
            geometry,
            ScreenGeometry(0, 0, 1920, 1080, "per-monitor-v2", 96),
        )
        self.assertEqual(
            [call.args[0] for call in user32.GetSystemMetrics.call_args_list],
            [0, 1],
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
