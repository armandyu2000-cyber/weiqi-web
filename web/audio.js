"use strict";

/* 背景音：自然音，一直在底下循环。
 *
 * 有件事做不到，写在最前面免得以后有人来「修」它：**打开页面自动响是不行的**。
 * 浏览器的 autoplay 策略会拦掉一切没经过用户手势的音频。所以这个声音是选出来的
 * —— 选的那一下就是浏览器要的手势。音量记着，但不自动播。
 *
 * 就是一个普通的 <audio loop>，没有 AudioContext、没有合成器。文件放在 nature/
 * 文件夹里（在数据目录下，打包后是 %LOCALAPPDATA%\Go\nature）。
 */

(function () {
  const $ = (id) => document.getElementById(id);
  const KEY = "go.audio.";

  const nature = new Audio();
  nature.preload = "none";
  nature.loop = true;              // 垫底的，不接下一首，一直循环

  const natureVol = () => Number($("nature-vol").value) / 100;
  const bareName = (f) => f.replace(/\.[^.]+$/, "");

  function playNature(name) {
    $("nature-pick").value = name || "";
    if (!name) {
      nature.pause();              // 不 clear src：清了会触发一次 "empty src" 报错
      render();
      return;
    }
    nature.src = "/nature/" + encodeURIComponent(name);
    // play() 返回的 promise 被拒，多半是格式浏览器解不了（比如 Safari 遇到 Ogg）。
    // 不接的话它就是一个没人管的 rejection。
    nature.play().catch((err) =>
      toast(`这个自然音放不了：${name}（${err.message}）`, true));
  }

  // 事件反过来驱动标题，这样「正在放」永远是听得到的事实，不是我们以为的状态。
  function render() {
    $("audio-now").textContent =
      nature.paused ? "没在放" : bareName($("nature-pick").value);
  }

  nature.addEventListener("play", render);
  nature.addEventListener("pause", render);

  $("nature-vol").oninput = () => {
    nature.volume = natureVol();
    localStorage.setItem(KEY + "natureVol", $("nature-vol").value);
  };

  (async function init() {
    $("nature-vol").value = localStorage.getItem(KEY + "natureVol") || "45";
    nature.volume = natureVol();

    let data;
    try {
      data = await api("/api/nature");
    } catch (err) {
      toast(`自然音列表没读出来：${err.message}`, true);
      return;
    }

    const files = data.files || [];
    const off = document.createElement("option");
    off.value = "";
    off.textContent = files.length ? "（不放）" : "（还没有自然音）";
    $("nature-pick").appendChild(off);

    for (const name of files) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = bareName(name);
      $("nature-pick").appendChild(opt);
    }

    // 文件夹路径挂在 title 上：页面上不占地方，想知道往哪儿丢文件时悬停一下就有。
    $("nature-pick").title = `把音频文件丢进这个文件夹：\n${data.dir}`;

    // 不记选择：这是「选一下就出声」的开关，记了会留下一个「选中了却没在放」
    // 的状态，看着像坏了。音量才是值得记的那个。
    $("nature-pick").onchange = (e) => playNature(e.target.value);

    render();
  })();
})();
