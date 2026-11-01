let config = null;

export const loadPresentationConfig = async () => {
  try {
    const response = await fetch("/config.json", { cache: "no-store" });
    if (response.ok) config = await response.json();
  } catch {
    config = null;
  }
};

export const hasPresentationConfig = () => Boolean(
  config &&
  ["apiUrl", "cognitoDomain", "clientId", "redirectUri"].every(
    (key) => typeof config[key] === "string" && config[key].length > 0 && !config[key].includes("YOUR_"),
  ),
);

export const getPresentationConfig = () => {
  if (!hasPresentationConfig()) {
    throw new Error("Sign-in needs the AWS settings in config.js.");
  }
  for (const key of ["apiUrl", "cognitoDomain"]) {
    if (new URL(config[key]).protocol !== "https:") {
      throw new Error("The API and sign-in service must use HTTPS.");
    }
  }
  return config;
};
