#!/usr/bin/env python3
"""
光遇每日任务 README 更新脚本
从 Cloudflare Worker 获取数据并更新 README.md
"""

import hashlib
import io
import os
import sys
import requests
import re
from datetime import datetime, timezone, timedelta

# 从环境变量获取配置
WORKER_URL = os.environ.get('WORKER_URL')
API_SECRET = os.environ.get('API_SECRET')

# 图片落盘目录（仓库内相对路径，GitHub 直出，不走 camo 代理）
IMAGES_DIR = 'images'
IMAGE_MAX_WIDTH = 1200
UA = 'SkyDailyInfo/1.0 (+https://github.com/A-Kevin1217/SkyDailyInfo)'

def localize_image(url):
    """把外链图片落到仓库内，返回相对路径。

    GitHub 渲染 README 时会把外链图片改写成 camo.githubusercontent.com 代理，
    国内访问经常超时/504，表现为图片加载不出来。仓库内相对路径由 GitHub 直出，
    稳定得多（本机实测 0.9s 内可加载）。
    """
    if not isinstance(url, str) or not url.startswith('http'):
        return url

    digest = hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]
    if os.path.isdir(IMAGES_DIR):
        cached = [f for f in os.listdir(IMAGES_DIR) if f.startswith(digest + '.')]
        if cached:
            return f"{IMAGES_DIR}/{cached[0]}"

    try:
        resp = requests.get(url, timeout=60, headers={'User-Agent': UA})
        resp.raise_for_status()
        data = resp.content
    except requests.exceptions.RequestException as e:
        print(f"⚠️ 图片本地化失败，沿用原链接: {url} ({e})")
        return url

    ext = '.jpg' if re.search(r'\.jpe?g(\?|$)', url, re.IGNORECASE) else '.png'

    # 有 Pillow 就顺手压一下体积，没有就存原图
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.width > IMAGE_MAX_WIDTH:
                ratio = IMAGE_MAX_WIDTH / float(im.width)
                im = im.resize((IMAGE_MAX_WIDTH, max(1, int(im.height * ratio))), Image.LANCZOS)
            if im.mode in ('RGBA', 'LA', 'P'):
                rgba = im.convert('RGBA')
                if rgba.getchannel('A').getextrema() == (255, 255):
                    im = rgba.convert('RGB')
                    ext = '.jpg'
            buf = io.BytesIO()
            if ext == '.jpg':
                im.convert('RGB').save(buf, format='JPEG', quality=85, optimize=True)
            else:
                im.save(buf, format='PNG', optimize=True)
            data = buf.getvalue()
    except Exception as e:
        print(f"ℹ️ 跳过压缩（{type(e).__name__}），使用原图")

    name = digest + ext
    os.makedirs(IMAGES_DIR, exist_ok=True)
    with open(os.path.join(IMAGES_DIR, name), 'wb') as f:
        f.write(data)
    print(f"   🖼 {name} ({len(data) // 1024}KB)")
    return f"{IMAGES_DIR}/{name}"

def localize_payload(data):
    """把 payload 里所有图片地址换成仓库内相对路径。"""
    if not isinstance(data, dict):
        return data

    for detail in data.get('taskDetails') or []:
        detail['images'] = [localize_image(u) for u in (detail.get('images') or [])]

    calendar = data.get('calendar') or {}
    if calendar.get('images'):
        calendar['images'] = [localize_image(u) for u in calendar['images']]

    weather = data.get('weather')
    if isinstance(weather, dict) and weather.get('images'):
        weather['images'] = [localize_image(u) for u in weather['images']]

    return data

def prune_images(content):
    """清掉仓库里已不再引用的图片，避免仓库无限膨胀。"""
    if not os.path.isdir(IMAGES_DIR):
        return
    for name in os.listdir(IMAGES_DIR):
        if not name.startswith('_') and f"{IMAGES_DIR}/{name}" in content:
            continue
        try:
            os.remove(os.path.join(IMAGES_DIR, name))
            print(f"   🧹 清理未引用图片 {name}")
        except OSError:
            pass

def fetch_daily_data():
    """从 Cloudflare Worker 获取每日数据"""
    if not WORKER_URL or not API_SECRET:
        print("错误: 未设置 WORKER_URL 或 API_SECRET 环境变量")
        sys.exit(1)
    
    headers = {
        'Authorization': f'Bearer {API_SECRET}',
        'Content-Type': 'application/json'
    }
    
    try:
        print(f"正在请求 Worker: {WORKER_URL}")
        response = requests.get(WORKER_URL, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if not data.get('success'):
            print(f"Worker 返回错误: {data.get('error', '未知错误')}")
            sys.exit(1)
        
        # 显示缓存状态
        if data.get('cached'):
            print(f"✅ 使用缓存数据 (缓存时间: {data.get('cacheTime', 'N/A')})")
        else:
            print(f"🔄 从网易 API 获取新数据")
        
        return data['data']
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        sys.exit(1)

def extract_tasks(task_data):
    """提取任务列表（使用 Worker 已处理好的数据）"""
    # 如果有 taskList，直接格式化
    if 'taskList' in task_data and task_data['taskList']:
        tasks = []
        tasks.append('【今日旅行指南】')
        for task in task_data['taskList']:
            tasks.append(f"{task['number']}. {task['task']}")
        return '\n'.join(tasks)
    
    # 否则使用 rawAnswer
    return task_data.get('rawAnswer', '')

def format_events(events):
    """格式化活动列表"""
    if not events:
        return "今日暂无特殊活动"
    
    result = []
    for event in events:
        times = ', '.join(event['times'])
        result.append(f"**{event['title']}** - {event['description']}")
        result.append(f"- 时间: {times}")
        result.append(f"- 地点: {event['location']}")
        result.append("")
    
    return '\n'.join(result)

def format_weather(weather_data):
    """格式化天气预报 - 包含文字和图片"""
    if not weather_data:
        return None, None
    
    # 处理字典格式(包含 text 和 images)
    if isinstance(weather_data, dict):
        text = weather_data.get('text', '')
        images = weather_data.get('images', [])
        return text, images
    
    # 兼容旧的纯文本格式
    return str(weather_data), []

    # 清理 HTML 标签和特殊控制序列 (#r, #n 等)
    # 去掉 HTML
    clean = re.sub(r'<[^>]+>', '', raw)
    # 替换控制序列为换行
    clean = clean.replace('#r', '\n').replace('#n', '\n')
    # 去掉多余空白
    clean = re.sub(r'\s+', ' ', clean).strip()

    # 提取以“天气播报：”开头的短句，截断在常见分隔词处（如 如果, ===, 请）
    m = re.search(r'天气播报：\s*([^\n\r]+)', clean)
    if m:
        text = m.group(0)  # 包含“天气播报：”
        # 在可能的推广或额外提示前截断
        text = re.split(r'如果|===|请给|请帮|如上|点赞|感谢', text)[0].strip()
        return text

    # 回退策略：寻找第一句包含“天气”或“播报”的短句
    m2 = re.search(r'([^。\n\r]{0,100}(天气|播报)[^。\n\r]{0,100})', clean)
    if m2:
        return m2.group(1).strip()

    # 最后回退，截取前120字符作为展示
    return clean[:120].strip()

def format_task_details(details_list):
    """格式化任务详情（先祖位置等）"""
    if not details_list:
        return ""
    
    result = []
    for detail in details_list:
        keyword = detail.get('keyword', '')
        title = detail.get('title', keyword)
        
        result.append(f"\n#### 📍 {title}")
        
        # 添加文字内容
        text = detail.get('text', '')
        if text:
            result.append(f"\n{text}\n")
        
        # 添加图片
        images = detail.get('images', [])
        if images:
            result.append("")  # 空行
            for i, img_url in enumerate(images):
                result.append(f"![{keyword}-{i+1}]({img_url})")
        
        result.append("\n---\n")  # 分隔线
    
    return '\n'.join(result)

def format_calendar(calendar_data):
    """格式化日历图片"""
    if not calendar_data:
        return ""
    
    images = calendar_data.get('images', [])
    if not images:
        return ""
    
    # 显示第一张日历图片
    return f"![光遇日历]({images[0]})"

def update_readme(task_data, events_data, weather_data, task_details=None, calendar_data=None):
    """更新 README.md 文件"""
    readme_path = 'README.md'
    
    # 读取现有 README
    try:
        with open(readme_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print("README.md 不存在，将创建新文件")
        content = ""
    
    # 获取北京时间
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    date_str = now.strftime('%Y年%m月%d日')
    time_str = now.strftime('%H:%M:%S')
    
    # 提取任务内容
    tasks = extract_tasks(task_data)
    
    # 格式化活动
    events = format_events(events_data)
    
    # 格式化天气 (返回文字和图片)
    weather_text, weather_images = format_weather(weather_data)
    
    # 格式化任务详情
    details = format_task_details(task_details) if task_details else ""
    
    # 格式化日历
    calendar = format_calendar(calendar_data) if calendar_data else ""
    
    # 生成天气部分
    weather_section = ""
    if weather_text:
        weather_section = f"""
### 🌤️ 天气预报

{weather_text}

"""
        # 添加天气图片
        if weather_images:
            for img_url in weather_images:
                weather_section += f"![天气预报]({img_url})\n\n"
    
    # 生成日历部分
    calendar_section = ""
    if calendar:
        calendar_section = f"""
### 📅 本月日历

{calendar}

"""
    
    # 生成任务详情部分
    details_section = ""
    if details:
        details_section = f"""
### 📖 任务详细攻略

{details}
"""
    
    new_section = f"""## 📅 {date_str} 每日任务

> 最后更新: {date_str} {time_str} (北京时间)

### 🎯 今日旅行指南

```
{tasks}
```
{weather_section}{calendar_section}{details_section}
### 🎪 今日活动

{events}

---

"""
    
    # 替换或插入内容
    # 查找标记位置
    start_marker = "<!-- DAILY_TASK_START -->"
    end_marker = "<!-- DAILY_TASK_END -->"
    
    if start_marker in content and end_marker in content:
        # 替换现有内容
        pattern = f"{re.escape(start_marker)}.*?{re.escape(end_marker)}"
        new_content = re.sub(
            pattern,
            f"{start_marker}\n{new_section}{end_marker}",
            content,
            flags=re.DOTALL
        )
    else:
        # 如果没有标记，在文件末尾添加
        if not content.strip().endswith('---'):
            content += '\n\n---\n\n'
        new_content = content + f"\n{start_marker}\n{new_section}{end_marker}\n"
    
    # 写入文件
    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print(f"✅ README.md 已更新 ({date_str} {time_str})")
    if task_details:
        print(f"   📍 包含 {len(task_details)} 个任务详情")
    if calendar_data:
        print(f"   📅 包含本月日历")

    prune_images(new_content)

def main():
    print("🌤 开始更新光遇每日任务...")
    
    # 获取数据
    data = localize_payload(fetch_daily_data())
    print("✅ 成功获取数据")
    
    # 更新 README
    update_readme(
        data['task'], 
        data['events'], 
        data.get('weather'),
        data.get('taskDetails'),
        data.get('calendar')
    )
    print("✅ 完成!")

if __name__ == '__main__':
    main()
