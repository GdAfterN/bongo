import { Config, CubismSetting, Live2DSprite } from "easy-live2d";
import { Application, Ticker } from "pixi.js";

Config.MouseFollow = false;
Config.MotionSound = false;

const stage = document.querySelector("#stage");
const canvas = document.querySelector("#live2d");
const keyOverlay = document.querySelector("#key-overlay");
const errorBox = document.querySelector("#error");
let model = null;
let pixiApp = null;
let resizeModel = () => {};
let modelWidth = 0;
let modelHeight = 0;
let currentKey = "";
let pulseTimer = 0;

keyOverlay.addEventListener("error", () => {
  keyOverlay.style.display = "none";
});

function modelUrl(file = "") {
  return new URL(`model/${file}`, window.location.href).href;
}

function setParameter(id, value) {
  model?.setParameterValueById(id, Number(value));
}

function rangeValue(id, ratio, invert = false) {
  const range = model?.getParameterValueRangeById(id);
  if (!range) return;
  const value = range.max - ratio * (range.max - range.min);
  setParameter(id, invert ? -value : value);
}

window.bongoPet = {
  syncViewport(requestedResolution) {
    if (!pixiApp) return false;
    const resolution = Math.max(
      1,
      Number(requestedResolution) || Number(window.devicePixelRatio) || 1,
    );
    if (Math.abs(pixiApp.renderer.resolution - resolution) > 0.001) {
      pixiApp.renderer.resolution = resolution;
    }
    pixiApp.renderer.resize(
      Math.max(1, stage.clientWidth),
      Math.max(1, stage.clientHeight),
    );
    resizeModel();
    return true;
  },
  setMirror(mirrored) {
    stage.style.transform = mirrored ? "scaleX(-1)" : "none";
  },
  setKey(key, pressed) {
    if (!/^[A-Za-z0-9]+$/.test(key)) return;
    if (pressed) {
      currentKey = key;
      keyOverlay.src = modelUrl(`resources/left-keys/${key}.png`);
      keyOverlay.style.display = "block";
      setParameter("CatParamLeftHandDown", 1);
    } else if (currentKey === key) {
      currentKey = "";
      keyOverlay.style.display = "none";
      setParameter("CatParamLeftHandDown", 0);
    }
  },
  setMouseButton(button, pressed) {
    setParameter(button === "right" ? "ParamMouseRightDown" : "ParamMouseLeftDown", pressed);
  },
  lookAt(x, y) {
    const xRatio = Math.max(0, Math.min(1, (x + 1) / 2));
    const yRatio = Math.max(0, Math.min(1, (y + 1) / 2));
    rangeValue("ParamMouseX", xRatio);
    rangeValue("ParamMouseY", yRatio);
    rangeValue("ParamAngleX", xRatio);
    rangeValue("ParamAngleY", yRatio);
    rangeValue("ParamAngleZ", xRatio * yRatio);
    rangeValue("ParamEyeBallX", xRatio);
    rangeValue("ParamEyeBallY", yRatio);
  },
  pulse(action) {
    window.clearTimeout(pulseTimer);
    const pressed = action === "left";
    setParameter("CatParamLeftHandDown", pressed ? 1 : 0);
    pulseTimer = window.setTimeout(() => setParameter("CatParamLeftHandDown", 0), 450);
  },
};

async function start() {
  const app = new Application();
  await app.init({
    view: canvas,
    resizeTo: stage,
    backgroundAlpha: 0,
    antialias: true,
    autoDensity: true,
    resolution: window.devicePixelRatio,
  });
  pixiApp = app;

  const modelJSON = await fetch(modelUrl("cat.model3.json")).then((response) => response.json());
  const setting = new CubismSetting({ modelJSON });
  setting.redirectPath(({ file }) => modelUrl(file));
  model = new Live2DSprite({ modelSetting: setting, ticker: Ticker.shared });
  app.stage.addChild(model);
  await model.ready;
  modelWidth = model.width;
  modelHeight = model.height;

  resizeModel = () => {
    if (modelWidth <= 0 || modelHeight <= 0) return;
    const scale = Math.min(stage.clientWidth / modelWidth, stage.clientHeight / modelHeight);
    model.scale.set(scale);
    model.anchor.set(0.5);
    model.x = stage.clientWidth / 2;
    model.y = stage.clientHeight / 2;
  };
  window.addEventListener("resize", () => window.bongoPet.syncViewport());
  window.bongoPet.syncViewport();
  document.body.dataset.ready = "true";
}

start().catch((error) => {
  errorBox.textContent = `BongoCat load failed: ${String(error)}`;
  errorBox.style.display = "block";
});
