'use strict';

const copyButton = document.getElementById('copyScript');
const source = document.getElementById('scriptSource');
const status = document.getElementById('copyStatus');

async function loadScript() {
  try {
    const response = await fetch('/widgets/keqi-widget.js', { cache: 'no-store' });
    if (!response.ok) throw new Error('load failed');
    const text = await response.text();
    if (!text.includes('CalendarEvent.between') || !text.includes('Script.setWidget')) throw new Error('invalid script');
    source.value = text;
    copyButton.textContent = '复制小组件脚本';
    copyButton.disabled = false;
  } catch {
    copyButton.textContent = '脚本暂时无法载入';
    status.textContent = '请刷新此页重试，或从 Mac 项目的 widgets 文件夹取得“课期.js”。';
  }
}

copyButton.addEventListener('click', async () => {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(source.value);
      status.textContent = '已复制。首次安装请新建脚本；升级请替换原“课期”脚本的全部内容，再运行一次。';
      return;
    }
  } catch { /* Local HTTP on iPhone may not expose the Clipboard API. */ }
  document.getElementById('sourceDetails').open = true;
  source.focus();
  source.select();
  source.setSelectionRange(0, source.value.length);
  let copied = false;
  try { copied = document.execCommand('copy'); } catch { /* Select for manual copy below. */ }
  status.textContent = copied ? '已复制。首次安装请新建脚本；升级请替换原“课期”脚本的全部内容，再运行一次。' : '脚本已选中，请长按并选择“拷贝”，再到 Scriptable 替换原“课期”脚本的全部内容并运行。';
});

loadScript();
