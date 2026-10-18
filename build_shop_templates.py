"""从参考截图切出营业小游戏的识别模板，写入 assets/。

参考图会被缩放到规范 3840x2160 再切，保证和运行时同一尺度。

用法: python build_shop_templates.py
"""
import os

import cv2

import nte_shop as S

# (输出文件名, 在规范空间里的裁剪框, 说明)
CROPS = [
    ("shop_demand_tomato.png", (1858, 408, 2002, 540), "需求气泡里的番茄汁图标"),
    ("shop_cup.png", (3385, 1372, 3805, 1768), "台面右侧三只透明玻璃杯"),
    ("shop_tomato.png", (3298, 1138, 3492, 1382), "番茄汁利乐包"),
]


def main():
    os.makedirs(S.ASSETS, exist_ok=True)

    srcs = {}
    for name in ("ref_demand.jpg", "ref_no_customer.jpg"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        img = cv2.imread(p)
        if img is None:
            raise SystemExit(f"读不到 {p}")
        srcs[name] = S.canon(img)

    # 气泡模板来自有客人的那张
    demand = srcs["ref_demand.jpg"]
    for fname, box, desc in CROPS:
        x0, y0, x1, y1 = box
        crop = demand[y0:y1, x0:x1]
        cv2.imwrite(os.path.join(S.ASSETS, fname), crop)
        print(f"{fname:28} {desc}  {crop.shape[1]}x{crop.shape[0]}")


if __name__ == "__main__":
    main()
