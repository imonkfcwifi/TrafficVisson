export const PRODUCT_IDENTITY = Object.freeze({
  appUserModelId: "kr.co.trafficsupervision.desktop",
  productName: "TrafficSuperVision",
});

export function applyProductIdentity(
  electronApp,
  platform = process.platform,
) {
  electronApp.setName(PRODUCT_IDENTITY.productName);
  if (platform === "win32") {
    electronApp.setAppUserModelId(PRODUCT_IDENTITY.appUserModelId);
  }
}
