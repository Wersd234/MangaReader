import discord
from discord.ext import commands, tasks
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
import json
from dotenv import load_dotenv

# ================= 1. 环境变量读取 =================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NHENTAI_API_KEY = os.getenv("NHENTAI_API_KEY")

if not DISCORD_TOKEN or not NHENTAI_API_KEY:
    raise ValueError("❌ 启动失败：未找到 DISCORD_TOKEN 或 NHENTAI_API_KEY")

# ================= 2. 路径与网络配置 =================
SAVE_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_downloads")
INDEX_FILE = os.path.join(SAVE_DIRECTORY, "_bot_index.json")

API_BASE = "https://nhentai.net/api/v2"

HEADERS = {
    "Authorization": f"Key {NHENTAI_API_KEY}",
    "User-Agent": "MyPrivateDiscordBot/5.0 (XML ID Indexing)"
}

CDN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://nhentai.net/"
}

PREFIX_PATTERN = re.compile(r"^nhentai-\d+\s*-\s*")


# ================= 3. 全局辅助函数 =================
async def resolve_query_to_id(query: str):
    if query.isdigit():
        return query
    async with bot.session.get(f"{API_BASE}/search", headers=HEADERS, params={"query": query}) as resp:
        if resp.status == 200:
            data = await resp.json()
            if data.get("result"):
                return str(data["result"][0]["id"])
    return None


def get_id_from_cbz(filepath: str) -> str:
    """⚡ 神级提取：不解压直接从 CBZ 内部的 ComicInfo.xml 提取 6位数 ID"""
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            for item in z.infolist():
                if item.filename.lower() == 'comicinfo.xml':
                    xml_str = z.read(item).decode('utf-8', errors='ignore')
                    # 正则匹配 <Web> 标签里的链接，提取 6位数 ID
                    match = re.search(r'<Web>.*?nhentai\.net/g/(\d+)/?</Web>', xml_str, re.IGNORECASE)
                    if match:
                        return match.group(1)
    except Exception as e:
        print(f"[Warning] 无法从 {os.path.basename(filepath)} 读取 ID: {e}")
    return None


# ================= 4. 核心 Bot 类 (精准 ID 索引) =================
class NhentaiBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session = None
        # 内存索引字典 { "6位数ID": "实际文件名.cbz" }
        self.local_index = {}

    def load_index_from_disk(self):
        if os.path.exists(INDEX_FILE):
            try:
                with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                    self.local_index = json.load(f)
                print(f"[Index] 💾 已从 JSON 恢复索引，库藏 {len(self.local_index)} 本。")
                return True
            except Exception as e:
                print(f"[Index] ⚠️ JSON 损坏，将重新扫描: {e}")
        return False

    def save_index_to_disk(self):
        try:
            with open(INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.local_index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Index] ❌ 保存 JSON 失败: {e}")

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()

        if not os.path.exists(SAVE_DIRECTORY):
            os.makedirs(SAVE_DIRECTORY, exist_ok=True)

        if not self.load_index_from_disk():
            await self.sync_index_task()

        self.sync_index_task.start()
        print("✅ Docker Bot 启动成功！斜杠指令已同步。")

    @tasks.loop(minutes=30)
    async def sync_index_task(self):
        """每 30 分钟后台静默提取硬盘上所有 CBZ 的 6位数 ID"""
        temp_index = {}
        for filename in os.listdir(SAVE_DIRECTORY):
            if filename.lower().endswith('.cbz'):
                filepath = os.path.join(SAVE_DIRECTORY, filename)
                # 放在后台线程去拆包提取 XML，防卡顿
                gal_id = await asyncio.to_thread(get_id_from_cbz, filepath)
                if gal_id:
                    temp_index[gal_id] = filename

        if temp_index != self.local_index:
            self.local_index = temp_index
            await asyncio.to_thread(self.save_index_to_disk)
            print(f"[Index] 🔄 硬盘 XML 索引已同步！最新库藏: {len(self.local_index)} 本。")

    @sync_index_task.before_loop
    async def before_sync(self):
        await self.wait_until_ready()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


bot = NhentaiBot()


# ================= 5. Kavita 刮削逻辑 =================
def process_downloaded_cbz(filepath: str) -> str:
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


# ================= 6. Discord 交互视图 =================
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
            page_filename = self.pages[self.current_page]
            img_data = await asyncio.to_thread(self._read_local_page, self.local_filepath, page_filename)
            ext = page_filename.split('.')[-1]
        else:
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
        file = discord.File(io.BytesIO(img_data), filename=filename, spoiler=self.is_public)
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


# ================= 7. 核心指令 =================
@bot.tree.command(name="cache", description="下载本子、自动去前缀、修复 Kavita 元数据")
@app_commands.describe(query="6位ID或搜索词")
async def cache_gallery(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)

    target_id = await resolve_query_to_id(query)
    if not target_id:
        return await interaction.followup.send("❌ 找不到对应的本子。")

    # ✨ XML ID 瞬间查重拦截！连 API 都不要调用了！
    if target_id in bot.local_index:
        return await interaction.followup.send(
            f"✅ 该本子已经在库中，无需重复下载！\n📂 已存文件：`{bot.local_index[target_id]}`")

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
            with open(final_filepath, 'wb') as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    f.write(chunk)

        final_filepath_after_process = await asyncio.to_thread(process_downloaded_cbz, final_filepath)
        result_filename = os.path.basename(final_filepath_after_process)

        # ✨ 下载完成后：使用 6位数ID 直接存入索引
        bot.local_index[target_id] = result_filename
        await asyncio.to_thread(bot.save_index_to_disk)
        print(f"[Index] ➕ 成功存入 XML ID 索引: {target_id} -> {result_filename}")

        await interaction.followup.send(
            f"✅ 下载并修复成功！\n📂 存档: `{result_filename}`\n👉 可以去 Kavita 里面强制扫描了。")

    except Exception as e:
        await interaction.followup.send(f"❌ 发生错误: {str(e)}")


@bot.tree.command(name="read", description="在 Discord 阅读本子 (基于 XML ID 极速秒开)")
@app_commands.describe(query="6位数ID或搜索词", public="公开显示(带马赛克) 还是 仅自己可见(无码)")
async def read_gallery(interaction: discord.Interaction, query: str, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    target_id = await resolve_query_to_id(query)
    if not target_id:
        return await interaction.followup.send("❌ 找不到本子。")

    # ✨ 核心：通过 6 位数 ID 直接查找本地！
    local_filepath = None
    if target_id in bot.local_index:
        local_filename = bot.local_index[target_id]
        potential_path = os.path.join(SAVE_DIRECTORY, local_filename)
        if os.path.exists(potential_path):
            local_filepath = potential_path
            print(f"[Debug] ⚡ XML ID [{target_id}] 匹配成功！加载本地文件...")

    if local_filepath:
        def get_cbz_pages():
            with zipfile.ZipFile(local_filepath, 'r') as z:
                return sorted(
                    [f for f in z.namelist() if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))])

        local_pages = await asyncio.to_thread(get_cbz_pages)
        if not local_pages:
            return await interaction.followup.send("❌ 本地压缩包已损坏或没有图片。")

        view = GalleryReaderView(cdn_url=None, pages=local_pages, is_public=public, local_filepath=local_filepath)
        file = await view.get_current_page_file()
        title = os.path.basename(local_filepath)

        await interaction.followup.send(
            content=f"💾 **[本地极速读取] {title}**\n第 1 / {len(local_pages)} 页",
            file=file,
            view=view
        )
    else:
        # 走网络模式
        async with bot.session.get(f"{API_BASE}/galleries/{target_id}", headers=HEADERS) as gal_resp:
            if gal_resp.status != 200:
                return await interaction.followup.send(f"❌ 无法获取本子详情，状态码: {gal_resp.status}")
            gal_data = await gal_resp.json()
            pages = gal_data.get("pages", gal_data.get("images", {}).get("pages", []))

        if not pages:
            return await interaction.followup.send("❌ 无法解析本子的页面数据。")

        async with bot.session.get(f"{API_BASE}/cdn", headers=HEADERS) as cdn_resp:
            cdn_url = (await cdn_resp.json()).get("url",
                                                  "https://i.nhentai.net") if cdn_resp.status == 200 else "https://i.nhentai.net"

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