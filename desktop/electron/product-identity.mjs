export const PRODUCT_IDENTITY = Object.freeze({
  appUserModelId: "kr.co.trafficvisson.desktop",
  productName: "TrafficVisson",
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
