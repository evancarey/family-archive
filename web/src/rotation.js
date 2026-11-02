export const rotateAngle = (currentAngle, degrees) =>
  ((currentAngle + degrees) % 360 + 360) % 360;

export const archiveEntryKey = (item) => `${item.source_id}:${item.path}:${item.sha256}`;

export const imageRotationKey = (item) => item.sha256 || archiveEntryKey(item);

export const imageRotationFor = (rotations, item) =>
  rotations[imageRotationKey(item)] ?? rotations[archiveEntryKey(item)] ?? 0;
