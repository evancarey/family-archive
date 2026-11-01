export const rotateAngle = (currentAngle, degrees) =>
  ((currentAngle + degrees) % 360 + 360) % 360;
