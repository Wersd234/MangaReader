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


def generate_comicinfo_xml(gal_data, gal_id: str) -> str:
    """根据 API 返回的数据，自动生成完美的 ComicInfo.xml"""

    # ✨ 核心修复：优先使用 english，它包含了完整的汉化组中文字符
    title_dict = gal_data.get('title', {})
    title = title_dict.get('english') or title_dict.get('japanese') or title_dict.get('pretty') or f"Gallery_{gal_id}"


    # 解析标签、作者、语言
    artists = []
    tags = []
    language = "ja"  # 默认日语

    for t in gal_data.get('tags', []):
        t_type = t.get('type')
        t_name = t.get('name')
        if t_type == 'artist' or t_type == 'group':
            artists.append(t_name)
        elif t_type == 'tag':
            tags.append(t_name)
        elif t_type == 'language':
            if t_name == 'chinese':
                language = 'zh'
            elif t_name == 'english':
                language = 'en'

    writer = ", ".join(artists) if artists else "Unknown"
    tags_str = ", ".join(tags)
    page_count = gal_data.get('num_pages', 0)

    # 拼装为标准的 Kavita XML 格式
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{title}</Title>
  <Series>{title}</Series>
  <Writer>{writer}</Writer>
  <Tags>{tags_str}</Tags>
  <LanguageISO>{language}</LanguageISO>
  <Web>https://nhentai.net/g/{gal_id}/</Web>
  <PageCount>{page_count}</PageCount>
  <Manga>YesAndRightToLeft</Manga>
  <AgeRating>Adults Only 18+</AgeRating>
</ComicInfo>"""
    return xml


def inject_comicinfo_to_cbz(filepath: str, xml_str: str):
    """将生成的 XML 注入到下载好的 CBZ 压缩包中"""
    # 以追加模式 'a' 打开 zip，直接将 XML 塞入内部
    with zipfile.ZipFile(filepath, 'a', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('ComicInfo.xml', xml_str.encode('utf-8'))

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
    """完全使用你提供的逻辑：先去前缀，再处理 XML"""

    dirname = os.path.dirname(filepath)
    filename = os.path.basename(filepath)
    current_filepath = filepath

    # === 1. 你的原版去前缀逻辑 (remove_prefix) ===
    if PREFIX_PATTERN.search(filename):
        new_filename = PREFIX_PATTERN.sub("", filename)
        new_filepath = os.path.join(dirname, new_filename)
        if not os.path.exists(new_filepath):
            try:
                os.rename(filepath, new_filepath)
                current_filepath = new_filepath
                print(f"[Process] ✅ 成功移除前缀: {new_filename}")
            except Exception as e:
                print(f"[Process] ❌ 重命名失败 [{filename}]: {e}")
        else:
            print(f"[Process] ⚠️ 跳过重命名 [{filename}]: 目标文件名已存在")

    # === 2. 你的原版 XML 处理逻辑 (process_cbz_metadata) ===
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

                    # 获取 AlternateSeries 的内容
                    alt_match = re.search(r'<AlternateSeries>(.*?)</AlternateSeries>', xml_str,
                                          re.IGNORECASE | re.DOTALL)
                    if alt_match:
                        alt_text = alt_match.group(1)
                        # 如果存在 Series 标签，将其内容替换
                        if re.search(r'<Series>.*?</Series>', xml_str, re.IGNORECASE | re.DOTALL):
                            xml_str = re.sub(r'<Series>.*?</Series>', f'<Series>{alt_text}</Series>', xml_str,
                                             flags=re.IGNORECASE | re.DOTALL)
                        else:
                            # 如果没有 Series 标签，加上它
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
            print(f"[Process] ✅ 元数据修复成功: {os.path.basename(current_filepath)}")
        else:
            os.remove(temp_path)
            print(f"[Process] ⏩ 元数据无需修改: {os.path.basename(current_filepath)}")

        # 赋予读写权限，防 Kavita 报错
        os.chmod(current_filepath, 0o666)

    except Exception as e:
        print(f"[Error] ❌ 处理损坏或报错 [{os.path.basename(current_filepath)}]: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        # 如果彻底烂了，抛出异常删掉假文件
        if os.path.exists(current_filepath):
            os.remove(current_filepath)
        raise ValueError("ZIP 文件解压失败或不是合法压缩包。")

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
# ================= 7. 核心指令 =================
@bot.tree.command(name="cache", description="下载本子、自动命名并注入完美 Kavita 刮削数据")
@app_commands.describe(query="6位ID或搜索词")
async def cache_gallery(interaction: discord.Interaction, query: str):
    # 开启仅自己可见 (隐私保护)
    await interaction.response.defer(thinking=True, ephemeral=True)

    target_id = await resolve_query_to_id(query)
    if not target_id:
        return await interaction.followup.send("❌ 找不到对应的本子。")

    # ✨ 瞬间查重拦截
    if target_id in bot.local_index:
        return await interaction.followup.send(
            f"✅ 该本子已经在库中，无需重复下载！\n📂 已存文件：`{bot.local_index[target_id]}`")

    try:
        # ================= 1. 获取本子详情，生成完美标题 =================
        async with bot.session.get(f"{API_BASE}/galleries/{target_id}", headers=HEADERS) as gal_resp:
            if gal_resp.status != 200:
                return await interaction.followup.send(f"❌ 无法获取本子详情，状态码: {gal_resp.status}")
            gal_data = await gal_resp.json()

            # ✨ 核心修复：文件名也同样优先使用 english 全称
            title_dict = gal_data.get('title', {})
            raw_title = title_dict.get('english') or title_dict.get('japanese') or title_dict.get(
                'pretty') or f"Gallery_{target_id}"

            safe_title = re.sub(r'[\\/*?:"<>|]', "", raw_title).strip()
            final_filename = f"{safe_title}.cbz"
            final_filepath = os.path.join(SAVE_DIRECTORY, final_filename)

        # ================= 2. 获取专属下载直链 =================
        download_api_url = f"{API_BASE}/galleries/{target_id}/download"
        async with bot.session.post(download_api_url, headers=HEADERS) as resp:
            if resp.status != 200:
                return await interaction.followup.send(f"❌ 获取下载链接失败，API 状态码: {resp.status}")

            resp_json = await resp.json()
            actual_download_url = resp_json.get("url")
            if not actual_download_url:
                return await interaction.followup.send("❌ API 未返回真实的下载链接！")

        # ================= 3. 开始流式下载真实的 ZIP 文件 =================
        async with bot.session.get(actual_download_url, headers=CDN_HEADERS) as resp:
            if resp.status != 200:
                return await interaction.followup.send(f"❌ 下载压缩包失败，CDN 状态码: {resp.status}")

            content_type = resp.headers.get('Content-Type', '').lower()
            if 'html' in content_type or 'text' in content_type:
                fake_html = await resp.text()
                return await interaction.followup.send(f"❌ 下载被拦截！拿到的是网页。\n前100字符: `{fake_html[:100]}`")

            # 写入硬盘
            with open(final_filepath, 'wb') as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    f.write(chunk)

        # ================= 4. 自己生成并注入最完美的 ComicInfo.xml =================
        xml_content = generate_comicinfo_xml(gal_data, target_id)
        await asyncio.to_thread(inject_comicinfo_to_cbz, final_filepath, xml_content)

        # 赋予权限给 Kavita (0o666 代表可读可写)
        os.chmod(final_filepath, 0o666)

        # ================= 5. 存入后台索引 =================
        result_filename = os.path.basename(final_filepath)
        bot.local_index[target_id] = result_filename
        await asyncio.to_thread(bot.save_index_to_disk)
        print(f"[Index] ➕ 成功注入 XML 并存入索引: {target_id} -> {result_filename}")

        await interaction.followup.send(
            f"✅ 下载并注入刮削数据成功！\n📂 存档: `{result_filename}`\n👉 等待几分钟去 NC 或者在 Kavita 里强制扫描即可。")

    except Exception as e:
        # 兜住所有上面可能发生的报错
        await interaction.followup.send(f"❌ 发生致命错误: {str(e)}")

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