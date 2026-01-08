#!/usr/bin/env python3
"""
HTML 海报导出工具 - Gradio 版
支持上传 HTML 文件（含本地图片资源）预览，选择 PPI 并导出为高分辨率 PNG 图片

两种输入方式：
1. 通过文件选择器选择本地 HTML 文件（推荐，支持相对路径图片资源）
2. 上传 ZIP 文件（包含 HTML 和资源文件）
"""

import sys
import io

# Windows 下设置 stdout 为 UTF-8 编码，避免 emoji 输出报错
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

import gradio as gr
import asyncio
import tempfile
import os
import shutil
import zipfile
import subprocess
import platform
from pathlib import Path

# 使用 Playwright 进行 HTML 渲染和截图
from playwright.async_api import async_playwright


def open_file_dialog_macos() -> str:
    """
    使用 AppleScript 调用 macOS 原生文件选择对话框
    这个方法可以在任何线程中调用
    """
    # AppleScript 命令
    script = '''
    tell application "System Events"
        activate
    end tell
    
    set theFile to choose file with prompt "选择 HTML 文件" of type {"html", "htm", "public.html"} default location (path to home folder)
    return POSIX path of theFile
    '''
    
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=120  # 2分钟超时
        )
        
        if result.returncode == 0:
            path = result.stdout.strip()
            if path:
                return path
        else:
            # 用户取消了选择
            print(f"文件选择取消或出错: {result.stderr}")
            
    except subprocess.TimeoutExpired:
        print("文件选择超时")
    except Exception as e:
        print(f"文件对话框错误: {e}")
    
    return ""


def open_file_dialog_windows() -> str:
    """
    使用 PowerShell 调用 Windows 原生文件选择对话框
    """
    # PowerShell 脚本
    script = '''
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "选择 HTML 文件"
    $dialog.Filter = "HTML 文件 (*.html;*.htm)|*.html;*.htm|所有文件 (*.*)|*.*"
    $dialog.InitialDirectory = [Environment]::GetFolderPath("UserProfile")
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        Write-Output $dialog.FileName
    }
    '''
    
    try:
        result = subprocess.run(
            ['powershell', '-Command', script],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if result.returncode == 0:
            path = result.stdout.strip()
            if path:
                return path
                
    except subprocess.TimeoutExpired:
        print("文件选择超时")
    except Exception as e:
        print(f"文件对话框错误: {e}")
    
    return ""


def open_file_dialog_linux() -> str:
    """
    Linux 使用 zenity 或 kdialog
    """
    # 尝试 zenity (GNOME)
    try:
        result = subprocess.run(
            ['zenity', '--file-selection', '--title=选择 HTML 文件', 
             '--file-filter=HTML files | *.html *.htm'],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Zenity 错误: {e}")
    
    # 尝试 kdialog (KDE)
    try:
        result = subprocess.run(
            ['kdialog', '--getopenfilename', os.path.expanduser('~'), 
             'HTML files (*.html *.htm)'],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"KDialog 错误: {e}")
    
    return ""


def browse_file():
    """点击浏览按钮时调用，根据操作系统选择对应的文件选择器"""
    system = platform.system()
    
    if system == "Darwin":  # macOS
        path = open_file_dialog_macos()
    elif system == "Windows":
        path = open_file_dialog_windows()
    else:  # Linux
        path = open_file_dialog_linux()
    
    if path:
        return path, f"✅ 已选择: {Path(path).name}"
    return gr.update(), "⏳ 未选择文件（点击浏览或直接粘贴路径）"

# 默认海报尺寸（与原 export.html 保持一致）
DEFAULT_WIDTH = 900
DEFAULT_HEIGHT = 1200

# PPI 选项
PPI_OPTIONS = {
    "72 PPI (屏幕预览)": 72,
    "150 PPI (普通打印)": 150,
    "300 PPI (高清印刷)": 300,
    "600 PPI (超清印刷)": 600,
}


def inject_snapshot_mode_css():
    """返回导出模式下需要注入的 CSS"""
    return """
    <style id="snapshot-mode-inject">
        .glass-panel {
            background: rgba(255, 255, 255, 0.96) !important; 
            backdrop-filter: none !important;
            -webkit-backdrop-filter: none !important;
            box-shadow: 0 8px 30px rgba(0,0,0,0.08) !important;
        }
        body, #poster {
            text-rendering: geometricPrecision; 
            -webkit-font-smoothing: antialiased;
        }
        #control-panel {
            display: none !important;
        }
    </style>
    """


async def render_html_file_to_image(
    html_file_path: str, 
    ppi: int = 300,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    poster_selector: str = "#poster",
) -> bytes:
    """
    直接从本地文件渲染 HTML（支持本地资源）
    使用 file:// 协议直接打开本地 HTML 文件
    """
    device_scale_factor = ppi / 96.0
    
    file_path = Path(html_file_path).resolve()
    file_url = f"file://{file_path}"
    
    print(f"📂 直接加载本地文件: {file_url}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        context = await browser.new_context(
            viewport={"width": width + 100, "height": height + 200},
            device_scale_factor=device_scale_factor,
        )
        
        page = await context.new_page()
        
        # 直接导航到本地文件
        await page.goto(file_url, wait_until="networkidle")
        
        # 注入快照模式 CSS
        await page.add_style_tag(content="""
            .glass-panel {
                background: rgba(255, 255, 255, 0.96) !important; 
                backdrop-filter: none !important;
                -webkit-backdrop-filter: none !important;
                box-shadow: 0 8px 30px rgba(0,0,0,0.08) !important;
            }
            body, #poster {
                text-rendering: geometricPrecision; 
                -webkit-font-smoothing: antialiased;
            }
            #control-panel {
                display: none !important;
            }
        """)
        
        # 等待字体加载
        await page.wait_for_timeout(2000)
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:
            pass
        
        # 截图
        poster_element = await page.query_selector(poster_selector)
        
        if poster_element:
            screenshot_bytes = await poster_element.screenshot(
                type="png",
                omit_background=False,
            )
        else:
            screenshot_bytes = await page.screenshot(
                type="png",
                full_page=False,
            )
        
        await browser.close()
        
        return screenshot_bytes


def sync_render_html_file_to_image(html_file_path: str, ppi: int = 300) -> bytes:
    """同步版本：直接渲染本地 HTML 文件"""
    return asyncio.run(render_html_file_to_image(html_file_path, ppi))


def extract_zip_to_temp(zip_path: str) -> tuple[Path, str]:
    """解压 ZIP 文件到临时目录"""
    temp_dir = Path(tempfile.mkdtemp(prefix="poster_export_"))
    
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(temp_dir)
    
    # 查找 HTML 文件
    html_files = list(temp_dir.rglob("*.html")) + list(temp_dir.rglob("*.htm"))
    
    if not html_files:
        raise ValueError("ZIP 文件中未找到 HTML 文件")
    
    # 优先选择根目录的 HTML
    html_file = html_files[0]
    for f in html_files:
        if f.parent == temp_dir:
            html_file = f
            break
        if 'poster' in f.name.lower() or 'index' in f.name.lower():
            html_file = f
    
    return temp_dir, str(html_file)


def process_local_path(local_path: str, ppi_choice: str):
    """处理本地 HTML 文件路径"""
    if not local_path or not local_path.strip():
        return None, None, "⚠️ 请输入 HTML 文件路径"
    
    local_path = local_path.strip()
    
    # 展开 ~ 为用户目录
    local_path = os.path.expanduser(local_path)
    
    if not os.path.exists(local_path):
        return None, None, f"❌ 文件不存在: {local_path}"
    
    if not local_path.lower().endswith(('.html', '.htm')):
        return None, None, "❌ 请输入 HTML 文件路径（.html 或 .htm）"
    
    try:
        ppi = PPI_OPTIONS.get(ppi_choice, 300)
        
        print(f"📄 处理本地文件: {local_path}")
        image_bytes = sync_render_html_file_to_image(local_path, ppi)
        
        # 保存输出
        output_filename = Path(local_path).stem
        with tempfile.NamedTemporaryFile(
            suffix=f"_{output_filename}_{ppi}PPI.png", 
            delete=False
        ) as tmp_file:
            tmp_file.write(image_bytes)
            output_path = tmp_file.name
        
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        img_width, img_height = img.size
        
        status = f"""✅ 导出成功！
📐 输出尺寸: {img_width} x {img_height} 像素
🎯 PPI: {ppi}
📁 文件大小: {len(image_bytes) / 1024 / 1024:.2f} MB
📄 源文件: {local_path}"""
        
        return output_path, output_path, status
        
    except Exception as e:
        import traceback
        return None, None, f"❌ 导出失败: {str(e)}\n{traceback.format_exc()}"


def preview_local_path(local_path: str):
    """预览本地 HTML 文件"""
    if not local_path or not local_path.strip():
        return None, "⏳ 请输入 HTML 文件路径"
    
    local_path = local_path.strip()
    local_path = os.path.expanduser(local_path)
    
    if not os.path.exists(local_path):
        return None, f"❌ 文件不存在: {local_path}"
    
    if not local_path.lower().endswith(('.html', '.htm')):
        return None, "❌ 请输入 HTML 文件路径"
    
    try:
        print(f"👁️ 预览本地文件: {local_path}")
        image_bytes = sync_render_html_file_to_image(local_path, ppi=72)
        
        with tempfile.NamedTemporaryFile(suffix="_preview.png", delete=False) as tmp_file:
            tmp_file.write(image_bytes)
            preview_path = tmp_file.name
        
        return preview_path, f"✅ 预览已加载（72 PPI）\n📄 文件: {local_path}"
        
    except Exception as e:
        import traceback
        return None, f"❌ 预览失败: {str(e)}\n{traceback.format_exc()}"


def process_zip_upload(file_obj, ppi_choice: str):
    """处理上传的 ZIP 文件"""
    if file_obj is None:
        return None, None, "⚠️ 请先上传 ZIP 文件"
    
    temp_dir = None
    
    try:
        ppi = PPI_OPTIONS.get(ppi_choice, 300)
        file_path = file_obj if isinstance(file_obj, str) else file_obj.name
        
        if not file_path.lower().endswith('.zip'):
            return None, None, "❌ 请上传 ZIP 文件（包含 HTML 和资源）"
        
        print("📦 解压 ZIP 文件...")
        temp_dir, html_file_path = extract_zip_to_temp(file_path)
        
        print(f"📄 渲染 HTML: {html_file_path}")
        image_bytes = sync_render_html_file_to_image(html_file_path, ppi)
        
        # 保存输出
        with tempfile.NamedTemporaryFile(
            suffix=f"_export_{ppi}PPI.png", 
            delete=False
        ) as tmp_file:
            tmp_file.write(image_bytes)
            output_path = tmp_file.name
        
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        img_width, img_height = img.size
        
        status = f"""✅ 导出成功！
📐 输出尺寸: {img_width} x {img_height} 像素
🎯 PPI: {ppi}
📁 文件大小: {len(image_bytes) / 1024 / 1024:.2f} MB"""
        
        return output_path, output_path, status
        
    except Exception as e:
        import traceback
        return None, None, f"❌ 导出失败: {str(e)}\n{traceback.format_exc()}"
    
    finally:
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


def preview_zip_upload(file_obj):
    """预览上传的 ZIP 文件"""
    if file_obj is None:
        return None, "⏳ 请上传 ZIP 文件"
    
    temp_dir = None
    
    try:
        file_path = file_obj if isinstance(file_obj, str) else file_obj.name
        
        if not file_path.lower().endswith('.zip'):
            return None, "❌ 请上传 ZIP 文件"
        
        print("📦 预览 ZIP 文件...")
        temp_dir, html_file_path = extract_zip_to_temp(file_path)
        image_bytes = sync_render_html_file_to_image(html_file_path, ppi=72)
        
        with tempfile.NamedTemporaryFile(suffix="_preview.png", delete=False) as tmp_file:
            tmp_file.write(image_bytes)
            preview_path = tmp_file.name
        
        return preview_path, f"✅ 预览已加载（72 PPI）"
        
    except Exception as e:
        import traceback
        return None, f"❌ 预览失败: {str(e)}\n{traceback.format_exc()}"
    
    finally:
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


# ============ Gradio 界面 ============

custom_css = """
.gradio-container {
    max-width: 1400px !important;
    margin: auto !important;
}

.path-input input {
    font-family: monospace !important;
    font-size: 14px !important;
}
"""

with gr.Blocks(
    title="HTML 海报导出工具",
) as app:
    
    gr.HTML("""
    <div style="text-align: center; padding: 20px 0;">
        <h1 style="font-size: 2.5rem; font-weight: 900; margin-bottom: 8px;">
            <span style="background: linear-gradient(135deg, #0284c7 0%, #4f46e5 50%, #9333ea 100%); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;">
                📄 HTML 海报导出工具
            </span>
        </h1>
        <p style="color: #64748b; font-size: 1.1rem;">
            支持本地图片资源 · 可选 PPI · 高清导出
        </p>
    </div>
    """)
    
    with gr.Row():
        # 左侧：控制面板
        with gr.Column(scale=1):
            
            with gr.Tabs() as input_tabs:
                # Tab 1: 本地文件模式（推荐）
                with gr.Tab("📂 选择本地文件（推荐）", id="local"):
                    gr.Markdown("""
                    点击「浏览」选择 HTML 文件，自动识别相对路径的图片资源
                    """)
                    
                    with gr.Row():
                        local_path_input = gr.Textbox(
                            label="HTML 文件路径",
                            placeholder="点击右侧按钮选择文件，或直接粘贴路径",
                            elem_classes=["path-input"],
                            lines=1,
                            scale=4,
                        )
                        browse_btn = gr.Button("① 浏览", size="lg", scale=1)
                    
                    with gr.Row():
                        local_preview_btn = gr.Button("② 预览", variant="secondary", size="lg")
                        local_export_btn = gr.Button("③ 导出", variant="primary", size="lg")
                
                # Tab 2: ZIP 上传模式
                with gr.Tab("📦 上传 ZIP", id="zip"):
                    gr.Markdown("""
                    上传包含 HTML 和资源的 ZIP 文件（适合分享）
                    """)
                    
                    zip_input = gr.File(
                        label="选择 ZIP 文件",
                        file_types=[".zip"],
                        type="filepath",
                    )
                    
                    with gr.Row():
                        zip_preview_btn = gr.Button("② 预览", variant="secondary", size="lg")
                        zip_export_btn = gr.Button("③ 导出", variant="primary", size="lg")
            
            gr.Markdown("### ⚙️ 导出设置")
            
            ppi_dropdown = gr.Dropdown(
                choices=list(PPI_OPTIONS.keys()),
                value="300 PPI (高清印刷)",
                label="选择 PPI（分辨率）",
                info="PPI 越高，图片越清晰，文件越大"
            )
            
            status_text = gr.Textbox(
                label="状态",
                interactive=False,
                lines=2,
                value="⏳ 等待操作...",
            )
            
            download_file = gr.File(
                label="📥 下载导出的图片",
                visible=True,
            )
            
            gr.Markdown("""
            ---
            ### 📖 使用说明
            
            **方式一：选择本地文件（推荐）**
            1. 点击 **① 浏览** 选择 HTML 文件
            2. 点击 **② 预览** 查看效果
            3. 点击 **③ 导出** 生成高清图片
            
            **方式二：上传 ZIP 包**
            - 将 HTML 和图片资源打包成 ZIP
            - 适合分享给他人使用
            """)
        
        # 右侧：预览区域
        with gr.Column(scale=2):
            gr.Markdown("### 🖼️ 预览")
            
            preview_image = gr.Image(
                label="海报预览",
                type="filepath",
                height=800,
            )
    
    # ===== 事件绑定 =====
    
    # 浏览按钮 - 打开文件选择器
    browse_btn.click(
        fn=browse_file,
        inputs=[],
        outputs=[local_path_input, status_text],
    )
    
    # 本地路径模式
    local_preview_btn.click(
        fn=preview_local_path,
        inputs=[local_path_input],
        outputs=[preview_image, status_text],
    )
    
    local_export_btn.click(
        fn=process_local_path,
        inputs=[local_path_input, ppi_dropdown],
        outputs=[preview_image, download_file, status_text],
    )
    
    # 输入路径后按回车预览
    local_path_input.submit(
        fn=preview_local_path,
        inputs=[local_path_input],
        outputs=[preview_image, status_text],
    )
    
    # ZIP 上传模式
    zip_preview_btn.click(
        fn=preview_zip_upload,
        inputs=[zip_input],
        outputs=[preview_image, status_text],
    )
    
    zip_export_btn.click(
        fn=process_zip_upload,
        inputs=[zip_input, ppi_dropdown],
        outputs=[preview_image, download_file, status_text],
    )
    
    # 上传 ZIP 时自动预览
    zip_input.change(
        fn=preview_zip_upload,
        inputs=[zip_input],
        outputs=[preview_image, status_text],
    )


def main():
    """主函数"""
    print("🚀 正在启动 HTML 海报导出工具...")
    print("📦 首次运行可能需要安装 Playwright 浏览器...")
    
    # 检查并安装 playwright 浏览器
    try:
        import subprocess
        result = subprocess.run(
            ["playwright", "install", "chromium"],
            check=True,
            capture_output=True,
            text=True,
        )
        print("✅ Playwright 浏览器已就绪")
    except Exception as e:
        print(f"⚠️ 请手动运行: playwright install chromium")
        print(f"   错误信息: {e}")
    
    # 启动应用
    app.launch(
        server_name="localhost",
        server_port=7860,
        share=False,
        inbrowser=True,
        css=custom_css,
        theme=gr.themes.Soft(
            primary_hue="indigo",
            secondary_hue="blue",
        ),
    )


if __name__ == "__main__":
    main()
