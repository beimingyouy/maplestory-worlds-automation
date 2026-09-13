# 木棉单特征模板风格

Use `D:\PythonProject4\img\自定义\木棉` as the local quality reference.

## Typical sizes

| Feature | Typical size |
|---|---|
| One eye | 12–14 px wide × 12–14 px high |
| One small face detail | 12–18 px wide × 9–14 px high |
| Mouth or teeth | 18–26 px wide × 8–14 px high |
| Wide teeth/face detail | up to 34–39 px wide × 15–18 px high |
| Hair tuft, horn, ornament | 12–18 px wide × 10–16 px high |

## Rules

1. Put only one eye in an eye crop. Do not include both eyes when either eye can stand alone.
2. Put only the mouth/teeth in a mouth crop. Avoid combining it with an eye.
3. Crop one hair tuft, horn, hat edge, or ornament at a time.
4. Keep a small amount of surrounding outline/color context for OpenCV matching.
5. Avoid entire heads, large body regions, generic wood texture, flat color, transparent margins, effects, and duplicates.
6. Use the mirrored original when the opposite-facing feature is clearer or when monsters can face both directions.

Reject and recrop if an eye crop contains two eyes, a mouth crop is mostly cheek/body color, a hair crop is mostly transparency, or the crop can match common map scenery.
