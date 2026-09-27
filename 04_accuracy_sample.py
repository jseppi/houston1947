# Blind stratified accuracy sample for zones_px_v2.tif: 8 random points per predicted class,
# rendered as unlabeled full-res tiles (ids only) + legend strip, for visual judging.
import numpy as np, csv
from osgeo import gdal
from PIL import Image, ImageDraw
rng=np.random.default_rng(1947)
z=gdal.Open('zones_px_v2.tif'); S=z.GetGeoTransform()[1]; a=z.ReadAsArray()
src=gdal.Open('11139001.jpg')
L='ABCDEFGHIJ'; pts=[]
for c in range(1,11):
    rr,cc=np.nonzero(a==c)
    for i in rng.choice(len(rr),8,replace=False): pts.append((int(cc[i]*S+S/2),int(rr[i]*S+S/2),L[c-1]))
order=rng.permutation(len(pts)); pts=[pts[i] for i in order]
T=180
def tile(x,y):
    x0=max(0,x-T//2); y0=max(0,y-T//2)
    t=np.dstack([src.GetRasterBand(b).ReadAsArray(x0,y0,T,T) for b in (1,2,3)])
    im=Image.fromarray(t); d=ImageDraw.Draw(im); cx,cy=x-x0,y-y0
    d.rectangle([cx-12,cy-12,cx+12,cy+12],outline=(255,0,0),width=2); return im
with open('accuracy_blind.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['id','x','y','pred'])
    for i,(x,y,p) in enumerate(pts): w.writerow([i,x,y,p])
for sheet in range(2):
    im=Image.new('RGB',(8*(T+6),5*(T+20)+140),'white'); d=ImageDraw.Draw(im)
    for k in range(40):
        i=sheet*40+k; x,y,_=pts[i]; r,c=divmod(k,8)
        im.paste(tile(x,y),(c*(T+6),r*(T+20)+20)); d.text((c*(T+6)+2,r*(T+20)+4),f'#{i}',fill=(0,0,0))
    # legend swatch strip
    oy=5*(T+20)+10
    for j,l in enumerate(L):
        sx,sy=9513,[3555,3624,3692,3760,3829,3897,3965,4034,4103,4171][j]
        t=np.dstack([src.GetRasterBand(b).ReadAsArray(sx-30,sy-25,60,50) for b in (1,2,3)])
        im.paste(Image.fromarray(t).resize((120,100)),(j*140,oy+20)); d.text((j*140+50,oy+2),l,fill=(0,0,0))
    im.save(f'accuracy_blind_{sheet}.png')
print('ok',len(pts))
