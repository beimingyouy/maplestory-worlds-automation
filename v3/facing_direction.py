"""蘑菇 V2 的人物面向识别。

识别器使用用户提供的左右头部小图，在人物当前位置附近做灰度模板匹配。受击时
画面可能短暂变黑，因此低置信度帧只标记为不可靠，不覆盖上一次可靠朝向。
"""

import base64
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional


LEFT_FACING_TEMPLATE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACIAAAATCAIAAAB6A0idAAAC2klEQVRIDZWVMYoVQRCG"
    "q0GQNRC6AxONCgPzukIdYTpWcO3AA+xECmIwwQZitKE8DB4vEqNlIg8wR2g8gcwZSqqq"
    "p5/73F3YoZk306+pr/+qv3ogpVAKj2VY5n3JzESEGOwS+aNjrf+PEAIRjmXITExUskYo"
    "/soaQQdhyVwyZyYYx2Ech1J4mfdMxKQrHHPXHbYrM2WmGGNmUoxF9I06KbPuwDBl8I1"
    "MU3EMYgwh1PmeARfnimra8XZMjJGZsmMys6uZRsUQYTIV63IfZjcphkw6AOiui+46mxa"
    "X4gsym5qe1mkcHBNCKK+fy8o6aliX49gkQp0hAhAiEzrGwynGChNj3DDKBiZNqxbT609"
    "aGI++BT1iHFlnxWgUVMbHc0BEl9IxAKBJozbfMKUwm2e8/lJvkPy13TWZisEYyaR8/aB"
    "Ix/T6+yaYVAp3NaUMjmHClGLXccfDpsYwrmzDqFERLWPm6YZRd5vlNwylpE6759LN2+U"
    "F8OdW/1YYneueZtLaWBOZT5yUUpTloo15kAPLYbDXK6n7PpwhB5IDfXn70runew8AEKN"
    "5RPvXTGkkV8ekatLTsxM1y/TKYFPDLOZogMO7Z02Zt5Gp8RnHEGHDpBD8VNjUBHyR6lx"
    "Vlghzy6HMRZZTjEcUkdatG6bOteXNe4vMwYhRDzQ1mxv6WkQCXoQQxqIzda6K2ZL2+8f"
    "nJ48fyWGoc40xlqzO7l25232LT8/6jPresxlCsFNBu8v6Zi9VSYdpqPOkMyKd4Q+7T29"
    "UnOm4mqgHBQBdbFmNcWP7FlIKaEcTM2nQtUq9PimPRtfTemn3TZnn7QbG/vL5JtF//Lh"
    "0U/qzk44KWvSTj8LSFyiGMEboM1L3R7Z/Obz5U9Kz9ijoti+NSTmBVVmvd5fvmx3Wm2w"
    "3hWemlWTRMuSetwdg6u7qEgB+/fz+rxpZLlrSZF1UkH3KvB6eulahh5C0+FvB1B3W494"
    "9fwFXCdlFMRhvnAAAAABJRU5ErkJggg=="
)

RIGHT_FACING_TEMPLATE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAB8AAAATCAIAAADnDkEVAAACnElEQVQ4EZWUP67UMBDG"
    "xxYSehQgB4kGaLwUUHuP4CMkNUhAineARwUSokjxCkS1JYooVqkQ1coVB/ARLI6QMwzM"
    "jP9kn0B6RNZu1mv/5vM34wHvnPduoOGdswDgrKXhLE+6cfSDd3meF8wTpPCvodaotNad"
    "1vu9hQ3dCd0YIwGEPvhM946iGjjjrhHqwAS4elz9+PKJLnTrnWh33lljTJVf6M5a++EN"
    "yDwUeoXKSzkNacekM91Zoos5ntQ3c2pUAPjynukk3ggII5DYMmSS0YW+s+Syd1bojs8O"
    "ANaSOb5YL3pJO/teZJ5ZVCZVCioF3XWd3zNd5A+UVHL2j1Jjcm7FHJLNj/hWfv3lW/F"
    "DWSW6yxQyh23htNG2WjbGmM+vn+HicKHKMcZgOrYRDxivcOlx8Rh6eudxky62nNEtOUZ"
    "Z5md5+0heME6ZHidc+jg9zysARHv34OKMLkrFlhRSLRvPlY6IdT8AnNHDKH95bxCx60w"
    "Kaff0Ydfp5ow4nk2/fzHPX2sAABgHqtQUEi79vbt3fn3/1JwJo6h5N1LClL1CREwnrXX"
    "LqtC51lkai5VJADhM5Dhti9P88VVDSwJ4cQrTMvWCxnRs9b4VThTes9We7a7JXCOuCeU"
    "zHbemKaUwnXCl60SuWukqpRa3uqguucDbZOYmptfP2BYU9G7XUXeqXUVMb+vWKKLm60t"
    "cSc7thwiX3pdvprOWSrUePx1//vgGAPPh+vZcWam1HrzTWmfTqFFZU+5CKWf29H/RSim"
    "54UQXQ4jN14ZqmS5bDvBi95izFHMOiz85z+UnK8hrlKL+rrXGpc90Y7I/eVuO0cIUx0"
    "4c5kawSArY0tpnmM59Roi1c8lp8p2ME4Zxe5oKKvGk55wEEmMS7dR2DvY3rvjkUtbfLp"
    "gAAAAASUVORK5CYII="
)

# 角色星星装饰的两个真实方向模板。用户已明确标注：第一张向左、第二张
# 向右。朝向融合以这两张星星为准，不再通过镜像推测另一侧。
LEFT_STAR_AUX_TEMPLATE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABIAAAAPCAIAAABm5AhFAAABKElEQVQ4EY1TQZHEMAwT"
    "hVAIBVMIhJZCIPQgFEIh3BRCMRiCIWSMoTdy0l739h67o9nxZiPZllpITiJpLhkA0vQp"
    "iiQS/uEUJEJVe/GiGITycsSeBRBA3BtpUfM7yZCIKmbLFU+kyb0dG9zb1Y1CFzjJRIIs"
    "RFkxb/j6dm+uOBV+BLP/m2vMVUCl4PCet/P+WDotB9J9RlFZkKdwL2hmZjuIg2ArCyjr"
    "gZDm2E8zzEx3PDGEQk53UNqMlFg3LAqjVPXY8A5q/SbB3bo5OSzKqrqveEfEkIEO6d1"
    "Gss8hmVgM/DeD0Wa4X/u97ieXlsW99d3cWyoRTw+Wu0lF4Y3ufuS2Yl5ZlHVYb4nMeb"
    "sCqEGTSrJcT8n9kxHzvJMjsbjDbuz78YN/vSI/fsYSXh+tISkAAAAASUVORK5CYII="
)

RIGHT_STAR_AUX_TEMPLATE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABQAAAAQCAIAAACZeshMAAABVklEQVQ4EZWSDY0EIQyF"
    "XyWwErCABSQwFkYCJ2EkIOGChNFQCUiYoGEubYHdy22yOfJC5qdfoX0FXPogjFXLXspeS"
    "147PpCvqQEi0hS5apb/wC7RIy2+lP0tHOEiM8PJg2qUJrDyRLRFj/nbggIwxMy9X+NVU"
    "gg/YEkRiQgresYFS9f7dRYI7xL8biK/i+z8R1DYGaBBISPk3q9+4mZ0Vv7rG1tBPChk0"
    "eDjrNknhIx43Gs1dzevcuubJO3X5CPgE3yyr7LbaSzH3k3UGe1UVbRKrbUJp9Ww1HSxR"
    "DzFFa8SchQszVvwcIWZueIsb8TM9AhEQXaR1Lzs8YBI8AN/JTB5gW2Xbi9Yev7LJ7tw7"
    "9e6uV47ismqde1pZsguilVWufhszmmfuZJ2e7ht8HQ4Hm4rvV93G/YgHqJNpUYOt2Kms"
    "NuQ6PTZGIVsriLsqjwfZMhkPMJTP4tOMj6DBdsvAAAAAElFTkSuQmCC"
)


class CharacterFacingObservation(NamedTuple):
    """保存一帧人物朝向识别结果及上一次可靠朝向。"""

    detected_at: float
    detected_direction: Optional[str]
    reliable_direction: Optional[str]
    left_confidence: float
    right_confidence: float
    confidence_margin: float
    match_ms: float
    black_flicker_suspected: bool
    reliable: bool


class CharacterFacingTracker:
    """在人物附近识别左右朝向，并在黑闪或低置信度时保留可靠结果。"""

    def __init__(
        self,
        cv2,
        np,
        confidence_threshold: float = 0.80,
        margin_threshold: float = 0.05,
        aux_confidence_threshold: float = 0.76,
        aux_margin_threshold: float = 0.04,
        radius_x: int = 90,
        radius_y: int = 105,
        aux_radius_x: int = 24,
        aux_top_offset: int = 24,
        aux_bottom_offset: int = 64,
        template_directory=None,
    ):
        """优先加载用户左右朝向图，缺失时回退到源码内嵌模板。"""
        self.confidence_threshold = float(confidence_threshold)
        self.margin_threshold = float(margin_threshold)
        self.aux_confidence_threshold = float(aux_confidence_threshold)
        self.aux_margin_threshold = float(aux_margin_threshold)
        self.radius_x = int(radius_x)
        self.radius_y = int(radius_y)
        self.aux_radius_x = int(aux_radius_x)
        self.aux_top_offset = int(aux_top_offset)
        self.aux_bottom_offset = int(aux_bottom_offset)
        resource_root = (
            Path(template_directory)
            if template_directory is not None
            else (
                Path(sys._MEIPASS) / "resres"
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent / "resres"
            )
        )
        left_path = resource_root / "facing_left.png"
        right_path = resource_root / "facing_right.png"
        self.left_template = self._load_gray_template(
            cv2,
            np,
            left_path,
            LEFT_FACING_TEMPLATE_BASE64,
        )
        self.right_template = self._load_gray_template(
            cv2,
            np,
            right_path,
            RIGHT_FACING_TEMPLATE_BASE64,
        )
        self.left_aux_template = self._decode_gray_template(
            cv2,
            np,
            LEFT_STAR_AUX_TEMPLATE_BASE64,
        )
        self.right_aux_template = self._decode_gray_template(
            cv2,
            np,
            RIGHT_STAR_AUX_TEMPLATE_BASE64,
        )
        self.template_source = (
            "files:{}|{}".format(left_path.name, right_path.name)
            if left_path.is_file() and right_path.is_file()
            else "embedded_fallback"
        )
        self.last_reliable_direction = None
        self.last_reliable_at = 0.0
        self.last_aux_direction = None
        self.last_aux_left_confidence = 0.0
        self.last_aux_right_confidence = 0.0
        self.last_aux_confidence_margin = 0.0
        self.last_aux_reliable = False
        self.last_aux_roi = None
        self.last_decision_source = "none"
        self.last_observation = self.empty_observation()

    @staticmethod
    def _decode_gray_template(cv2, np, encoded: str):
        """把源码内嵌的PNG模板解码为灰度图，避免依赖临时剪贴板文件。"""
        raw = base64.b64decode(encoded)
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("人物朝向模板解码失败")
        return image

    @classmethod
    def _load_gray_template(cls, cv2, np, path: Path, fallback_encoded: str):
        """读取外部PNG朝向模板；文件无效时安全使用内嵌模板。"""
        if path.is_file():
            try:
                image = cv2.imdecode(
                    np.fromfile(str(path), dtype=np.uint8),
                    cv2.IMREAD_GRAYSCALE,
                )
                if image is not None:
                    return image
            except (OSError, ValueError):
                pass
        return cls._decode_gray_template(cv2, np, fallback_encoded)

    @staticmethod
    def empty_observation(detected_at: float = 0.0):
        """返回尚未取得可靠朝向时使用的空识别结果。"""
        return CharacterFacingObservation(
            float(detected_at), None, None, 0.0, 0.0, 0.0, 0.0, False, False
        )

    def _clear_aux_observation(self) -> None:
        """人物位置不可用时清空本帧星星诊断，避免日志沿用上一帧结果。"""
        self.last_aux_direction = None
        self.last_aux_left_confidence = 0.0
        self.last_aux_right_confidence = 0.0
        self.last_aux_confidence_margin = 0.0
        self.last_aux_reliable = False
        self.last_aux_roi = None
        self.last_decision_source = "none"

    @staticmethod
    def _match_template(cv2, gray_roi, template) -> float:
        """返回模板在人物局部区域中的最高灰度相关系数。"""
        if (
            template.shape[0] > gray_roi.shape[0]
            or template.shape[1] > gray_roi.shape[1]
        ):
            return 0.0
        response = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        _minimum, maximum, _minimum_location, _maximum_location = cv2.minMaxLoc(
            response
        )
        return float(maximum)

    def observe(self, cv2, np, frame, monitor, person_screen_position, detected_at=None):
        """识别当前帧朝向；人物丢失或黑闪时不覆盖上一可靠朝向。"""
        captured_at = float(detected_at or time.monotonic())
        started = time.perf_counter()
        if frame is None or person_screen_position is None:
            self._clear_aux_observation()
            observation = self.empty_observation(captured_at)._replace(
                reliable_direction=self.last_reliable_direction,
                match_ms=(time.perf_counter() - started) * 1000,
            )
            self.last_observation = observation
            return observation

        frame_height, frame_width = frame.shape[:2]
        center_x = int(round(person_screen_position[0] - monitor["left"]))
        center_y = int(round(person_screen_position[1] - monitor["top"]))
        left = max(0, center_x - self.radius_x)
        right = min(frame_width, center_x + self.radius_x)
        top = max(0, center_y - self.radius_y)
        bottom = min(frame_height, center_y + self.radius_y)
        roi = frame[top:bottom, left:right]
        if roi.size == 0:
            self._clear_aux_observation()
            observation = self.empty_observation(captured_at)._replace(
                reliable_direction=self.last_reliable_direction,
                match_ms=(time.perf_counter() - started) * 1000,
            )
            self.last_observation = observation
            return observation

        gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        left_confidence = self._match_template(cv2, gray_roi, self.left_template)
        right_confidence = self._match_template(cv2, gray_roi, self.right_template)
        best_direction = "left" if left_confidence >= right_confidence else "right"
        best_confidence = max(left_confidence, right_confidence)
        margin = abs(left_confidence - right_confidence)
        main_reliable = (
            best_confidence >= self.confidence_threshold
            and margin >= self.margin_threshold
        )
        # 星星只允许在“自己人物定位点正下方”窄区域内匹配。完整人物模板仍
        # 使用上方的大 ROI，但星星绝不扫描左右其他人物所在区域。
        aux_left = max(0, center_x - self.aux_radius_x)
        aux_right = min(frame_width, center_x + self.aux_radius_x)
        aux_top = max(0, center_y + self.aux_top_offset)
        aux_bottom = min(frame_height, center_y + self.aux_bottom_offset)
        aux_roi = frame[aux_top:aux_bottom, aux_left:aux_right]
        self.last_aux_roi = (
            aux_left,
            aux_top,
            aux_right,
            aux_bottom,
        )
        if aux_roi.size > 0:
            aux_gray_roi = cv2.cvtColor(aux_roi, cv2.COLOR_BGR2GRAY)
            aux_left_confidence = self._match_template(
                cv2,
                aux_gray_roi,
                self.left_aux_template,
            )
            aux_right_confidence = self._match_template(
                cv2,
                aux_gray_roi,
                self.right_aux_template,
            )
        else:
            aux_left_confidence = 0.0
            aux_right_confidence = 0.0
        aux_direction = (
            "left"
            if aux_left_confidence >= aux_right_confidence
            else "right"
        )
        aux_best_confidence = max(aux_left_confidence, aux_right_confidence)
        aux_margin = abs(aux_left_confidence - aux_right_confidence)
        aux_reliable = (
            aux_best_confidence >= self.aux_confidence_threshold
            and aux_margin >= self.aux_margin_threshold
        )
        self.last_aux_direction = aux_direction if aux_reliable else None
        self.last_aux_left_confidence = aux_left_confidence
        self.last_aux_right_confidence = aux_right_confidence
        self.last_aux_confidence_margin = aux_margin
        self.last_aux_reliable = aux_reliable

        decision_source = "none"
        detected_direction = None
        if aux_reliable:
            # 用户明确要求以左右星星为准：星星可靠时直接决定朝向，完整人物
            # 模板只用于一致性诊断，不再在冲突时覆盖星星结果。
            detected_direction = aux_direction
            decision_source = (
                "star+main"
                if main_reliable and best_direction == aux_direction
                else (
                    "star-override"
                    if main_reliable
                    else "star"
                )
            )
        elif main_reliable:
            detected_direction = best_direction
            decision_source = "main-fallback"
        reliable = detected_direction in ("left", "right")
        self.last_decision_source = decision_source
        # 受击黑闪会让两个模板同时明显失配；此帧只用于日志，不改可靠朝向。
        dark_ratio = float(np.mean(gray_roi <= 24))
        black_flicker_suspected = (
            not reliable
            and dark_ratio >= 0.16
            and best_confidence < self.confidence_threshold
            and aux_best_confidence < self.aux_confidence_threshold
        )
        if reliable:
            self.last_reliable_direction = detected_direction
            self.last_reliable_at = captured_at

        observation = CharacterFacingObservation(
            detected_at=captured_at,
            detected_direction=detected_direction,
            reliable_direction=self.last_reliable_direction,
            left_confidence=left_confidence,
            right_confidence=right_confidence,
            confidence_margin=margin,
            match_ms=(time.perf_counter() - started) * 1000,
            black_flicker_suspected=black_flicker_suspected,
            reliable=reliable,
        )
        self.last_observation = observation
        return observation
