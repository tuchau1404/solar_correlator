import os
import re


def fix_math_and_markdown(text: str) -> str:
  # 1. Đưa ký tự xuống dòng về chuẩn Unix
  c = text.replace("\r\n", "\n")

  # 2. Chuẩn hóa display math dính dòng: chuyển thành khối có dòng trống độc lập
  # Chỉ khớp khi một dòng độc lập chứa duy nhất $$...$$
  # c = re.sub(
  #     r"(?:(?<=\n)|(?<=\A))[ \t]*\$\$([^\n]+?)\$\$[ \t]*(?=\n|\Z)",
  #     r"\n\n$$\n\1\n$$\n\n",
  #     c,
  # )

  # 3. Thu gọn các khoảng trống thừa (tối đa 1 dòng trống)
#   c = re.sub(r"\n{3,}", "\n\n", c)
  return c


def run():
  # Đồng bộ README sang docs/index.md nếu có
  if os.path.exists("README.md"):
    with open("README.md", "r", encoding="utf-8") as f:
      readme = f.read()
    readme_synced = fix_math_and_markdown(readme.replace("(docs/", "(./"))
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.md", "w", encoding="utf-8", newline="\n") as f:
      f.write(readme_synced)

  # Quét và định dạng lại các file .md trong docs/
  for root, _, files in os.walk("docs"):
    for file in files:
      if file.endswith(".md"):
        path = os.path.join(root, file)
        with open(path, "r", encoding="utf-8") as f:
          content = f.read()
        formatted = fix_math_and_markdown(content)
        if formatted != content:
          with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(formatted)
          print(f"[✓] Đã chuẩn hóa: {path}")


if __name__ == "__main__":
  run()