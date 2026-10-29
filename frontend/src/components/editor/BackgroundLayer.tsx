'use client';
import { useEffect, useState } from 'react';
import { Layer, Image as KonvaImage } from 'react-konva';
import { useEditorStore } from '@/store/useEditorStore';

export default function BackgroundLayer() {
  const bgUrl     = useEditorStore(s => s.bgImageDataUrl);
  const opacity   = useEditorStore(s => s.bgImageOpacity);
  const [img, setImg] = useState<HTMLImageElement | null>(null);

  useEffect(() => {
    if (!bgUrl) { setImg(null); return; }
    const image = new window.Image();
    image.onload = () => setImg(image);
    image.src = bgUrl;
  }, [bgUrl]);

  if (!img) return null;

  return (
    <Layer listening={false}>
      <KonvaImage
        image={img}
        x={-(img.width  / 2)}
        y={-(img.height / 2)}
        width={img.width}
        height={img.height}
        opacity={opacity}
      />
    </Layer>
  );
}
