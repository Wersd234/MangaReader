import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import os
import io
import asyncio
import zipfile
import tempfile
import shutil
import re
import email.message
from dotenv import load_dotenv

# ================= 1. 环境变量读取 =================
# 兼容本地和 Docker Portainer 的高级环境变量注入
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NHENTAI_API_KEY = os.getenv("NHENTAI_API_KEY")

if not DISCORD_TOKEN or not NHENTAI_API_KEY:
    raise ValueError("❌ 启动失败：未找到 DISCORD_TOKEN 或 NHENTAI_API_KEY")

# ================= 2. 路径与网络配置 =================
# 在 Docker 容器内，__file__ 所在目录就是 /app
# 所以它会自动保存在 /app/local_downloads 里面，正对应我们在 compose 里的映射
SAVE_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_downloads")

API_BASE = "https://nhentai.net/api/v2"

HEADERS = {
    "Authorization": f"Key {NHENTAI_API_KEY}",
    "User-Agent": "MyPrivateDiscordBot/2.0 (Docker Edition)"
}

CDN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://nhentai.net/"
}

PREFIX_PATTERN = re.compile(r"^nhentai-\d+\s*-\s*")


# ================= 3. 核心 Bot 类 =================
class NhentaiBot(commands.Bot):
    def __init__(self):
        # 移除 message_content 权限，避免 Discord 开发者后台报错
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session = None

    async def setup_hook(self):
        # 启动全局高速连接池
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        print("✅ Docker Bot 启动成功！全局高速通道已建立，斜杠指令已同步。")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


bot = NhentaiBot()


# ================= 4. 辅助与刮削函数 =================
async def resolve_query_to_id(query: str):
    if query.isdigit():
        return query
    async with bot.session.get(f"{API_BASE}/search", headers=HEADERS, params={"query": query}) as resp:
        if resp.status == 200:
            data = await resp.json()
            if data.get("result"):
                return str(data["result"][0]["id"])
    return None


def process_downloaded_cbz(filepath: str) -> str:
    """Kavita XML 刮削修复逻辑 (后台线程运行)"""
    dirname = os.path.dirname(filepath)
    filename = os.path.basename(filepath)
    current_filepath = filepath

    if PREFIX_PATTERN.search(filename):
        new_filename = PREFIX_PATTERN.sub("", filename)
        new_filepath = os.path.join(dirname, new_filename)
        if not os.path.exists(new_filepath):
            try:
                os.rename(filepath, new_filepath)
                current_filepath = new_filepath
            except:
                pass

    temp_fd, temp_path = tempfile.mkstemp(suffix='.cbz')
    os.close(temp_fd)
    modified = False

    try:
        with zipfile.ZipFile(current_filepath, 'r') as zin, zipfile.ZipFile(temp_path, 'w',
                                                                            zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename.lower() == 'comicinfo.xml':
                    xml_bytes = zin.read(item.filename)
                    xml_str = xml_bytes.decode('utf-8', errors='ignore')

                    alt_match = re.search(r'<AlternateSeries>(.*?)</AlternateSeries>', xml_str,
                                          re.IGNORECASE | re.DOTALL)
                    if alt_match:
                        alt_text = alt_match.group(1)
                        if re.search(r'<Series>.*?</Series>', xml_str, re.IGNORECASE | re.DOTALL):
                            xml_str = re.sub(r'<Series>.*?</Series>', f'<Series>{alt_text}</Series>', xml_str,
                                             flags=re.IGNORECASE | re.DOTALL)
                        else:
                            xml_str = xml_str.replace(
                                f'<AlternateSeries>{alt_text}</AlternateSeries>',
                                f'<Series>{alt_text}</Series>\n<AlternateSeries>{alt_text}</AlternateSeries>'
                            )
                        zout.writestr(item, xml_str.encode('utf-8'))
                        modified = True
                    else:
                        zout.writestr(item, xml_bytes)
                else:
                    zout.writestr(item, zin.read(item.filename))

        if modified:
            shutil.move(temp_path, current_filepath)
        else:
            os.remove(temp_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return current_filepath


# ================= 5. Discord 交互视图 (双擎模式) =================
class GalleryReaderView(discord.ui.View):
    def __init__(self, cdn_url, pages, is_public: bool, local_filepath: str = None):
        super().__init__(timeout=1800)
        self.cdn_url = cdn_url
        self.pages = pages
        self.current_page = 0
        self.is_public = is_public
        self.local_filepath = local_filepath

    def _read_local_page(self, filepath, page_filename):
        with zipfile.ZipFile(filepath, 'r') as z:
            return z.read(page_filename)

    async def get_current_page_file(self):
        if self.local_filepath:
            # 💾 本地极速模式
            page_filename = self.pages[self.current_page]
            img_data = await asyncio.to_thread(self._read_local_page, self.local_filepath, page_filename)
            ext = page_filename.split('.')[-1]
        else:
            # ☁️ 网络 API 模式
            page_info = self.pages[self.current_page]
            path = page_info.get("path")

            cdn_url_clean = self.cdn_url.rstrip("/")
            if cdn_url_clean.startswith("//"):
                cdn_url_clean = "https:" + cdn_url_clean

            if path:
                img_url = f"{cdn_url_clean}/{path.lstrip('/')}"
                ext = path.split('.')[-1]
            else:
                img_url = f"{cdn_url_clean}/galleries/unknown/{self.current_page + 1}.jpg"
                ext = "jpg"

            async with bot.session.get(img_url, headers=CDN_HEADERS) as resp:
                img_data = await resp.read()

        filename = f"SPOILER_page_{self.current_page}.{ext}" if self.is_public else f"page_{self.current_page}.{ext}"
        file = discord.File(
            io.BytesIO(img_data),
            filename=filename,
            spoiler=self.is_public
        )
        return file

    @discord.ui.button(label="⬅️ 上一页", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.defer()
            file = await self.get_current_page_file()
            await interaction.edit_original_response(content=f"第 {self.current_page + 1} / {len(self.pages)} 页",
                                                     attachments=[file], view=self)

    @discord.ui.button(label="下一页 ➡️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            await interaction.response.defer()
            file = await self.get_current_page_file()
            await interaction.edit_original_response(content=f"第 {self.current_page + 1} / {len(self.pages)} 页",
                                                     attachments=[file], view=self)


# ================= 6. 核心指令 =================
@bot.tree.command(name="cache", description="下载本子、自动去前缀、修复 Kavita 元数据")
@app_commands.describe(query="6位ID或搜索词")
async def cache_gallery(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    os.makedirs(SAVE_DIRECTORY, exist_ok=True)

    target_id = await resolve_query_to_id(query)
    if not target_id:
        return await interaction.followup.send("❌ 找不到对应的本子。")

    download_url = f"{API_BASE}/galleries/{target_id}/download"

    try:
        async with bot.session.post(download_url, headers=HEADERS) as resp:
            if resp.status != 200:
                return await interaction.followup.send(f"❌ 下载失败，API 状态码: {resp.status}")

            cd_header = resp.headers.get('Content-Disposition')
            if cd_header:
                msg = email.message.EmailMessage()
                msg['content-disposition'] = cd_header
                original_filename = msg.get_filename() or f"nhentai-{target_id} - unknown.zip"
            else:
                original_filename = f"nhentai-{target_id} - unknown.zip"

            if original_filename.endswith(".zip"):
                original_filename = original_filename[:-4] + ".cbz"

            final_filename = original_filename
            if PREFIX_PATTERN.search(final_filename):
                final_filename = PREFIX_PATTERN.sub("", final_filename)

            final_filepath = os.path.join(SAVE_DIRECTORY, final_filename)

            if os.path.exists(final_filepath):
                return await interaction.followup.send(
                    f"✅ 该本子已经在库中，无需重复下载！\n📂 已存文件：`{final_filename}`")

            with open(final_filepath, 'wb') as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    f.write(chunk)

        final_filepath_after_process = await asyncio.to_thread(process_downloaded_cbz, final_filepath)
        result_filename = os.path.basename(final_filepath_after_process)

        await interaction.followup.send(
            f"✅ 下载并修复成功！\n📂 存档: `{result_filename}`\n👉 现在可以去 Kavita 里面强制扫描了。")

    except Exception as e:
        await interaction.followup.send(f"❌ 发生错误: {str(e)}")


@bot.tree.command(name="read", description="在 Discord 阅读本子 (优先极速加载本地缓存)")
@app_commands.describe(query="6位数ID或搜索词", public="公开显示(带马赛克) 还是 仅自己可见(无码)")
async def read_gallery(interaction: discord.Interaction, query: str, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    target_id = await resolve_query_to_id(query)
    if not target_id:
        return await interaction.followup.send("❌ 找不到本子。")

        # ================= 1. 尝试寻找本地缓存文件 (带模糊匹配增强版) =================
        local_filepath = None
        download_url = f"{API_BASE}/galleries/{target_id}/download"

        try:
            async with bot.session.post(download_url, headers=HEADERS) as resp:
                if resp.status == 200:
                    cd_header = resp.headers.get('Content-Disposition')
                    if cd_header:
                        msg = email.message.EmailMessage()
                        msg['content-disposition'] = cd_header
                        original_filename = msg.get_filename() or ""

                        if original_filename.endswith(".zip"):
                            original_filename = original_filename[:-4] + ".cbz"

                        final_filename = original_filename
                        if PREFIX_PATTERN.search(final_filename):
                            final_filename = PREFIX_PATTERN.sub("", final_filename)

                        possible_path = os.path.join(SAVE_DIRECTORY, final_filename)

                        print(f"\n[Debug] === 开始本地查重 ID: {target_id} ===")
                        print(f"[Debug] 我预测的文件名是: {final_filename}")

                        # 严谨模式：精确匹配
                        if os.path.exists(possible_path):
                            print("[Debug] ✅ 严谨匹配成功！直接读取。")
                            local_filepath = possible_path
                        else:
                            print("[Debug] ❌ 严谨匹配失败，开启模糊匹配模式...")

                            # 模糊模式：剔除所有空格和标点符号，只比对字母、数字和中日文字符
                            def clean_str(s):
                                return re.sub(r'\W+', '', s).lower()

                            target_clean = clean_str(final_filename)

                            # 扫描目录下的所有文件
                            if os.path.exists(SAVE_DIRECTORY):
                                existing_files = os.listdir(SAVE_DIRECTORY)
                                print(f"[Debug] 当前挂载目录中共发现 {len(existing_files)} 个文件。")

                                for existing_file in existing_files:
                                    if clean_str(existing_file) == target_clean:
                                        print(f"[Debug] ✅ 模糊匹配成功！硬盘里的真实名字是: {existing_file}")
                                        local_filepath = os.path.join(SAVE_DIRECTORY, existing_file)
                                        break
                            else:
                                print(f"[Error] 严重错误：找不到映射的文件夹 {SAVE_DIRECTORY}")
        except Exception as e:
            print(f"[Error] 本地查重检测发生异常: {e}")

    # 2. 本地模式加载
    if local_filepath:
        def get_cbz_pages():
            with zipfile.ZipFile(local_filepath, 'r') as z:
                return sorted(
                    [f for f in z.namelist() if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))])

        pages = await asyncio.to_thread(get_cbz_pages)
        if not pages:
            return await interaction.followup.send("❌ 本地压缩包已损坏或没有图片。")

        view = GalleryReaderView(cdn_url=None, pages=pages, is_public=public, local_filepath=local_filepath)
        file = await view.get_current_page_file()
        title = os.path.basename(local_filepath)

        await interaction.followup.send(
            content=f"💾 **[本地极速读取] {title}**\n第 1 / {len(pages)} 页",
            file=file,
            view=view
        )
        return

    # 3. 网络模式加载
    async with bot.session.get(f"{API_BASE}/cdn", headers=HEADERS) as cdn_resp:
        cdn_url = (await cdn_resp.json()).get("url",
                                              "https://i.nhentai.net") if cdn_resp.status == 200 else "https://i.nhentai.net"

    async with bot.session.get(f"{API_BASE}/galleries/{target_id}", headers=HEADERS) as gal_resp:
        if gal_resp.status != 200:
            return await interaction.followup.send(f"❌ 无法获取本子详情，状态码: {gal_resp.status}")
        gal_data = await gal_resp.json()
        pages = gal_data.get("pages", gal_data.get("images", {}).get("pages", []))

    if not pages:
        return await interaction.followup.send("❌ 无法解析本子的页面数据。")

    view = GalleryReaderView(cdn_url, pages, is_public=public)
    file = await view.get_current_page_file()

    title = gal_data.get('title', {}).get('pretty', gal_data.get('title', {}).get('english', '未知标题'))
    await interaction.followup.send(
        content=f"☁️ **[网络 CDN 读取] {title}** (ID: {target_id})\n第 1 / {len(pages)} 页",
        file=file,
        view=view
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)