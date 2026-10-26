#!/usr/bin/env python3
"""Rebuild the two native SVG method figures. No third-party packages required."""
from pathlib import Path
from html import escape
import re

OUT = Path(__file__).resolve().parent.parent
BLACK, WHITE, RED = '#000000', '#FFFFFF', '#B32025'
SANS = 'Arial, Helvetica, sans-serif'
SERIF = 'Times New Roman, Times, serif'
MONO = 'Courier New, Courier, monospace'


def notation(value, size):
    out=[]
    for t in re.split(r'(E_[HR]|Q_[12]|k_G)', value):
        if re.fullmatch(r'E_[HR]|Q_[12]|k_G', t):
            a,b=t.split('_')
            out.append(escape(a)+f'<tspan baseline-shift="sub" font-size="{size*.72:g}">{escape(b)}</tspan>')
        else: out.append(escape(t))
    return ''.join(out)


class Figure:
    def __init__(self,name,height,title,desc):
        self.name=name;self.h=height;self.s=[]
        self.add(f'<svg xmlns="http://www.w3.org/2000/svg" width="180mm" height="{height*.18:g}mm" viewBox="0 0 1000 {height}" role="img" aria-labelledby="{name}-title {name}-desc">')
        self.add(f'<title id="{name}-title">{escape(title)}</title><desc id="{name}-desc">{escape(desc)}</desc>')
        self.add('<defs>')
        for col,key in [(BLACK,'black'),(RED,'red')]:
            self.add(f'<marker id="{name}-{key}" markerWidth="7" markerHeight="7" refX="6.5" refY="3.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M 0 0 L 7 3.5 L 0 7 Z" fill="{col}"/></marker>')
        self.add('</defs>');self.rect(0,0,1000,height,stroke='none',fill=WHITE)
    def add(self,t):self.s.append(t)
    def group(self,key,label):self.add(f'<g id="{self.name}-{key}" aria-label="{escape(label)}">')
    def end(self):self.add('</g>')
    def rect(self,x,y,w,h,stroke=BLACK,fill=WHITE,sw=1.1,r=0,dash=None):
        extra=f' stroke-dasharray="{dash}"' if dash else ''
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>')
    def circle(self,x,y,r=16,stroke=BLACK,dash=None,sw=1.25):
        extra=f' stroke-dasharray="{dash}"' if dash else ''
        self.add(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{WHITE}" stroke="{stroke}" stroke-width="{sw}"{extra}/>')
    def line(self,x1,y1,x2,y2,sw=1.1,color=BLACK,arrow=False,dash=None):
        marker=f' marker-end="url(#{self.name}-{"red" if color==RED else "black"})"' if arrow else ''
        extra=f' stroke-dasharray="{dash}"' if dash else ''
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{sw}"{marker}{extra}/>')
    def path(self,d,sw=1.1,color=BLACK,arrow=False):
        marker=f' marker-end="url(#{self.name}-{"red" if color==RED else "black"})"' if arrow else ''
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" stroke-linejoin="round"{marker}/>')
    def text(self,x,y,t,size=16,family=SANS,bold=False,italic=False,anchor='start',color=BLACK):
        self.add(f'<text x="{x}" y="{y}" fill="{color}" font-family="{family}" font-size="{size}" font-weight="{700 if bold else 400}" font-style="{"italic" if italic else "normal"}" text-anchor="{anchor}">{notation(t,size)}</text>')
    def math(self,x,y,t,size=20,**kw):self.text(x,y,t,size,family=SERIF,italic=True,**kw)
    def code(self,x,y,t,size=14,**kw):self.text(x,y,t,size,family=MONO,**kw)
    def title(self,x,y,label):self.text(x,y,label,17,bold=True,anchor='middle')
    def frame(self,x,w,label,y=8,h=250):
        self.rect(x,y,w,h,sw=.8);self.title(x+w/2,y+24,label);self.line(x,y+38,x+w,y+38,sw=.7)
    def arrow(self,x1,x2,y,lines=(),under=None):
        self.line(x1,y,x2,y,sw=1.35,arrow=True)
        for i,t in enumerate(lines):self.text((x1+x2)/2,y-16-(len(lines)-i-1)*17,t,14.5,anchor='middle')
        if under:self.math((x1+x2)/2,y+25,under,18,anchor='middle')
    def save(self,name):
        self.add('</svg>');(OUT/name).write_text('\n'.join(self.s)+'\n')


def expression(f,cx,y,mode='skeleton'):
    """Schematic expression structure; COUNT is an abbreviated subquery leaf."""
    xs=([cx-70,cx,cx+67] if mode=='skeleton' else [cx-82,cx-12,cx+65]);cy=y+65
    for i,x in enumerate(xs):
        inset=14 if i==0 else (17 if i==1 else (19 if mode=='skeleton' else 22))
        f.line(cx,y+14,x,cy-inset,sw=1.05)
    f.rect(cx-55,y-13,110,27,sw=1.1,r=2);f.code(cx,y+5,'coalesce',15.5,anchor='middle')
    f.rect(xs[0]-24,cy-14,48,28,sw=1.1,r=2);f.code(xs[0],cy+5,'NULL',15,anchor='middle')
    if mode=='skeleton':
        f.circle(xs[1],cy,17,dash='3 3');f.math(xs[1],cy+6,'L',20,anchor='middle')
        f.circle(xs[2],cy,19,dash='3 3');f.math(xs[2],cy+6,'E_H',19,anchor='middle')
    else:
        col=RED if mode=='base' else BLACK
        f.circle(xs[1],cy,17,stroke=col);f.math(xs[1],cy+6,'0',20,anchor='middle',color=col)
        # A bounded subtree explicitly carries the relationship-type reference.
        col=RED if mode=='base' else BLACK
        f.rect(xs[2]-47,cy-22,94,46,sw=1.1,r=2,stroke=col)
        f.code(xs[2],cy-3,'COUNT {…}',14.5,anchor='middle')
        f.code(xs[2],cy+16,':HIGH' if mode=='base' else ':REF',15,anchor='middle',color=RED if mode=='reference' else BLACK)
        if mode=='reference':f.line(xs[2]-18,cy+19,xs[2]+18,cy+19,color=RED,sw=1)


def bundle(f,x,y,n,role):
    # One representative node plus offset outlines denotes a target bundle.
    for dx,dy in [(8,-6),(4,-3),(0,0)]:f.circle(x+dx,y+dy,15,sw=1.1)
    f.math(x+47,y-7,role,21)
    f.text(x+47,y+16,f'×{n}',17)


f=Figure('algorithm1',270,'Graph state generation (Algorithm 1)',
 'Three panels show an S7 skeleton, its graph construction, and the generated specification. The non-null scalar role L remains a placeholder. A single root has eight HIGH targets and three REF targets, depicted as target bundles. The output G and construction conditions I are passed to Algorithm 2 and checked after materialization.')
f.group('skeleton','Skeleton and its semantic condition')
f.frame(8,254,'(a) Skeleton S')
expression(f,135,105)
f.math(135,214,'L ≠ NULL',18,anchor='middle')
f.math(135,242,'p = S7,  N = 2',17,anchor='middle')
f.end()
f.arrow(269,295,143)
f.group('graph','Graph construction with explicitly counted target bundles')
f.frame(302,414,'(b) Graph state G')
f.line(382,131,549,90,sw=1.25,arrow=True)
f.line(382,148,549,187,sw=1.25,arrow=True)
f.circle(365,139,18);f.math(365,145,'u',20,anchor='middle')
f.code(365,177,':Root',15,anchor='middle');f.code(365,196,'{id:1}',15,anchor='middle')
bundle(f,565,87,8,'H');bundle(f,565,190,3,'R')
f.code(468,95,'HIGH',15,anchor='middle');f.code(468,192,'REF',15,anchor='middle')
f.text(509,239,'Targets: mark = true',15,anchor='middle')
f.end()
f.arrow(723,749,143)
f.group('output','Construction specification and handoff to Algorithm 2')
f.frame(756,236,'(c) Output')
f.math(874,89,'(G, I)',27,anchor='middle')
f.math(790,126,'I:',19)
f.text(820,126,'one root',15.5)
f.math(820,154,'|H| = 8, |R| = 3',17)
f.line(874,175,874,202,sw=1.3,arrow=True)
f.text(874,232,'Algorithm 2',16.5,anchor='middle')
f.end();f.save('Algorithm_1_graph_state_generation.svg')

f=Figure('algorithm2',454,'Skeleton population and query mutation (Algorithm 2)',
 'Two aligned rows illustrate independent S7 and P1 instances. Each row proceeds from a skeleton to a base query and a mutated query. Graph information and construction conditions guide skeleton population. In S7, L is instantiated as zero and E_H as a COUNT subquery; reference substitution changes only the HIGH relationship type to REF. In P1, endpoint symmetry exchanges the written endpoint order, retaining each variable\'s labels, identifier constraint, and projection. Each mutated query is derived from a copy of its own base query. Dashed nodes are placeholders; red and explicit labels identify local changes.')
# Fixed column panels and baselines keep both examples comparable.
for x,label in [(8,'Skeleton S'),(378,'Base query Q_1'),(748,'Mutated query Q_2')]:
    f.frame(x,244,label,h=438)
    f.line(x,263,x+244,263,sw=.7)
# The output of Algorithm 1 enters the population step for the running example.
f.group('handoff','Graph information passed into skeleton population')
f.math(315,36,'(G, I)',19,anchor='middle')
f.line(315,50,315,91,sw=1.1,arrow=True)
f.end()
f.group('short-circuit','S7: placeholder population and reference substitution')
f.text(20,70,'S7 · Short-circuit pattern',15.5,bold=True)
for cx,mode in [(130,'skeleton'),(500,'base'),(870,'reference')]:expression(f,cx,119,mode)
f.arrow(265,365,150,['Skeleton','population'],'β')
f.arrow(635,735,150,['Reference','substitution'],'H ↦ R')
f.math(130,234,'L ≠ NULL',17.5,anchor='middle')
f.code(500,234,'value = 0',15,anchor='middle')
f.code(870,234,'value = 0',15,anchor='middle')
f.end()
f.group('symmetry','P1: local endpoint mirroring preserves variable constraints')
f.text(20,288,'P1 · Symmetry pattern',15.5,bold=True)
for cx,mode in [(130,'skeleton'),(500,'base'),(870,'mutated')]:
    left,right=cx-57,cx+57;cy=345
    f.line(left+17,cy,right-17,cy,sw=1.2)
    f.code(cx,cy-13,'T',15,anchor='middle')
    if mode=='skeleton':
        for x,t in [(left,'L'),(right,'H')]:
            f.circle(x,cy,17,dash='3 3');f.math(x,cy+6,t,20,anchor='middle')
    else:
        names=('u','v') if mode=='base' else ('v','u')
        for x,t in zip((left,right),names):
            f.circle(x,cy,17,stroke=RED);f.math(x,cy+6,t,20,anchor='middle',color=RED)
            f.code(x,cy+37,':Low' if t=='u' else ':High',15,anchor='middle')
            if t=='u':f.code(x,cy+55,'{id:1}',15,anchor='middle')
        f.code(cx,429,'RETURN u.uid, v.uid',14.5,anchor='middle')
f.arrow(265,365,345,['Skeleton','population'],'β')
f.arrow(635,735,345,['Symmetry','transformation'])
f.text(130,429,'Dashed nodes: placeholders',14.5,anchor='middle')
f.end();f.save('Algorithm_2_query_pair_construction.svg')
