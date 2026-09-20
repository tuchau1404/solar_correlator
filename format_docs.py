import os
import re


def fix_math_and_markdown(text: str) -> str:
  c = text.replace("\r\n", "\n")

  # 1. Tự động sửa khối display math nằm trong danh sách (thụt lề từ 1-3 space -> 4 spaces)
  def fix_list_math(match):
    prefix = match.group(1)  # khoảng trắng trước $$
    content = match.group(2)
    # Nếu đang thụt lề lơ lửng (1-3 spaces) bên trong danh sách, ép thành 4 spaces (chuẩn block list)
    indent = "    " if len(prefix) > 0 else ""
    return f"\n\n{indent}$$\n{indent}{content.strip()}\n{indent}$$\n\n"

  # Khớp toàn bộ khối $$...$$ nhiều dòng có thụt lề
  c = re.sub(r"\n([ \t]*)\$\$\n([\s\S]+?)\n[ \t]*\$\$", fix_list_math, c)

  # 2. Xử lý các dòng viết dính $$formula$$
  def expand_single_line_math(match):
    indent = "    " if len(match.group(1)) > 0 else ""
    formula = match.group(2).strip()
    return f"\n\n{indent}$$\n{indent}{formula}\n{indent}$$\n\n"

  c = re.sub(
      r"(?:(?<=\n)|(?<=\A))([ \t]*)\$\$(.+?)\$\$[ \t]*(?=\n|\Z)",
      expand_single_line_math,
      c,
  )

  # 3. Chuyển dấu gạch đứng | thành \vert chống vỡ bảng
  c = re.sub(
      r"\$([^\$\n]+?)\$",
      lambda m: f"${re.sub(r'(?<!\\)\|', r'\\vert ', m.group(1))}$",
      c,
  )

  # 4. Thu gọn dòng trống thừa
  c = re.sub(r"\n{3,}", "\n\n", c)
  return c


def run():
  if os.path.exists("README.md"):
    with open("README.md", "r", encoding="utf-8") as f:
      readme = f.read()
    readme_synced = fix_math_and_markdown(readme.replace("(docs/", "(./"))
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.md", "w", encoding="utf-8", newline="\n") as f:
      f.write(readme_synced)

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
          print(f"[✓] Đã sửa thụt lề: {path}")


if __name__ == "__main__":
  run()