import os
import re


def fix_math_and_markdown(text: str) -> str:
  # 1. Chuẩn hóa ký tự ngắt dòng Windows (CRLF) về Unix (LF)
  c = text.replace("\r\n", "\n")

  # 2. Xử lý các dòng công thức display math viết dính hoặc có thụt lề:
  #    Chuyển `   $$formula$$   ` thành khối 3 dòng chuẩn có khoảng cách trống
  def expand_display_math(match):
    formula = match.group(1).strip()
    return f"\n\n$$\n{formula}\n$$\n\n"

  # Khớp dòng chỉ chứa $$...$$ (chấp nhận khoảng trắng/tab ở đầu và cuối dòng)
  c = re.sub(
      r"(?:(?<=\n)|(?<=\A))[ \t]*\$\$(.+?)\$\$[ \t]*(?=\n|\Z)",
      expand_display_math,
      c,
  )

  # 3. Chuyển dấu gạch đứng | trong công thức toán thành \vert để không làm gãy bảng Markdown
  # Xử lý toán nội dòng $...$
  def fix_pipes_inline(match):
    inner = match.group(1)
    fixed = re.sub(r"(?<!\\)\|", r"\\vert ", inner)
    return f"${fixed}$"

  c = re.sub(r"\$([^\$\n]+?)\$", fix_pipes_inline, c)

  # Xử lý toán khối $$\n...\n$$
  def fix_pipes_block(match):
    inner = match.group(1)
    fixed = re.sub(r"(?<!\\)\|", r"\\vert ", inner)
    return f"$$\n{fixed}\n$$"

  c = re.sub(r"\$\$\n([\s\S]+?)\n\$\$", fix_pipes_block, c)

  # 4. Thu gọn các khoảng trắng dòng thừa (giữ tối đa 1 dòng trống)
  c = re.sub(r"\n{3,}", "\n\n", c)

  return c


def run():
  # Tự động đồng bộ README.md gốc vào docs/index.md để làm trang chủ
  if os.path.exists("README.md"):
    with open("README.md", "r", encoding="utf-8") as f:
      readme = f.read()

    # Chuyển link [Tên](docs/abc.md) thành link tương đối [Tên](./abc.md)
    readme_synced = readme.replace("(docs/", "(./")
    readme_synced = fix_math_and_markdown(readme_synced)

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.md", "w", encoding="utf-8", newline="\n") as f:
      f.write(readme_synced)
    print("[✓] Đã đồng bộ README.md -> docs/index.md")

  # Quét và tự động sửa toàn bộ file .md trong thư mục docs/
  modified_count = 0
  for root, _, files in os.walk("docs"):
    for file in files:
      if file.endswith(".md"):
        filepath = os.path.join(root, file)
        with open(filepath, "r", encoding="utf-8") as f:
          content = f.read()

        formatted = fix_math_and_markdown(content)

        if formatted != content:
          with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.write(formatted)
          print(f"[✓] Đã chuẩn hóa: {filepath}")
          modified_count += 1

  print(
      f"\nHoàn tất! Tổng cộng đã tối ưu định dạng {modified_count} tệp tài"
      " liệu."
  )


if __name__ == "__main__":
  run()