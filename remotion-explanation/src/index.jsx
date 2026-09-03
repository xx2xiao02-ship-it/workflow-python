import React from 'react';
import {AbsoluteFill, Composition, Video, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';

const palette = (style = {}) => ({
  background: style.background_color || '#F7F4EE',
  surface: style.surface_color || '#FFFFFF',
  text: style.text_color || '#172033',
  muted: style.muted_color || '#667085',
  accent: style.accent_color || '#D97745',
  accent2: style.accent_secondary || '#3B82A0',
});

const textStyle = (style, scale = 1) => ({
  fontFamily: style.font_family || 'Microsoft YaHei, Arial, sans-serif',
  fontSize: Math.max(18, Number(style.body_font_size || 34) * scale),
  color: style.text_color || '#172033',
  lineHeight: 1.25,
});

const Card = ({children, color, style, emphasis = false}) => (
  <div style={{
    background: color,
    borderRadius: 24,
    padding: '26px 30px',
    minWidth: 240,
    maxWidth: 520,
    boxShadow: emphasis ? '0 16px 32px rgba(23,32,51,.14)' : '0 8px 20px rgba(23,32,51,.08)',
    border: `2px solid ${emphasis ? style.accent : '#E6E1D8'}`,
    ...textStyle(style),
  }}>{children}</div>
);

const RelationCards = ({items, style, mode = 'row'}) => {
  const colors = palette(style);
  const values = (items || []).filter(Boolean).slice(0, 6);
  return <div style={{display: 'flex', flexDirection: mode === 'column' ? 'column' : 'row', gap: 22, alignItems: 'stretch', justifyContent: 'center', flexWrap: 'wrap'}}>
    {values.map((item, index) => <React.Fragment key={`${index}-${item}`}>
      <Card style={style} color={index % 2 ? '#EEF5F5' : colors.surface} emphasis={index === values.length - 1}>{item}</Card>
      {index < values.length - 1 && mode !== 'column' ? <div style={{alignSelf: 'center', color: colors.accent, fontSize: 50, fontWeight: 700}}>→</div> : null}
    </React.Fragment>)}
  </div>;
};

const Scene = ({template, props}) => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const style = props.style_lock || {};
  const colors = palette(style);
  const paceProfile = {
    slow: {introFrames: 20, progressDelay: 8},
    steady: {introFrames: 12, progressDelay: 0},
    fast: {introFrames: 6, progressDelay: 0},
  }[String(style.animation_pace || 'steady')] || {introFrames: 12, progressDelay: 0};
  const progress = interpolate(frame, [paceProfile.progressDelay, Math.max(paceProfile.progressDelay + 1, durationInFrames - 1)], [0, 1], {extrapolateRight: 'clamp'});
  const title = props.semantic_anchor || props.narration || '说明镜头';
  const objects = Array.isArray(props.relation_objects) && props.relation_objects.length ? props.relation_objects : [title];
  const common = {position: 'absolute', inset: 0, padding: '8% 7%', background: colors.background, ...textStyle(style)};
  const titleBox = {fontSize: Number(style.title_font_size || 58), fontWeight: 800, maxWidth: '84%', opacity: interpolate(frame, [0, paceProfile.introFrames], [0, 1], {extrapolateRight: 'clamp'}), transform: `translateY(${interpolate(frame, [0, paceProfile.introFrames], [18, 0], {extrapolateRight: 'clamp'})}px)`};
  let content;
  if (template === 'contrast_split') {
    content = <div style={{display: 'flex', gap: 40, justifyContent: 'center', alignItems: 'center', height: '58%'}}><Card style={style} color={colors.surface}>{objects[0] || title}</Card><div style={{fontSize: 68, color: colors.accent, fontWeight: 800}}>VS</div><Card style={style} color={colors.surface} emphasis>{objects[1] || objects[0] || title}</Card></div>;
  } else if (template === 'process_steps') {
    content = <div style={{height: '58%', display: 'flex', alignItems: 'center'}}><RelationCards items={objects} style={style}/></div>;
  } else if (template === 'hierarchy_layers') {
    content = <div style={{height: '58%', display: 'flex', alignItems: 'center', justifyContent: 'center'}}><RelationCards items={objects} style={style} mode="column"/></div>;
  } else if (template === 'timeline_path') {
    content = <div style={{height: '58%', display: 'flex', alignItems: 'center'}}><div style={{height: 10, width: '82%', background: colors.accent2, borderRadius: 99, position: 'relative'}}><div style={{position: 'absolute', left: `${progress * 82}%`, top: -17, width: 44, height: 44, borderRadius: 50, background: colors.accent}}/><div style={{position: 'absolute', top: 35, left: 0, right: 0, display: 'flex', justifyContent: 'space-between'}}>{objects.slice(0, 4).map((item, i) => <span key={`${i}-${item}`} style={{width: 180, textAlign: i === 0 ? 'left' : i === objects.slice(0, 4).length - 1 ? 'right' : 'center', ...textStyle(style, .72)}}>{item}</span>)}</div></div></div>;
  } else if (template === 'data_trend') {
    content = <div style={{height: '58%', display: 'flex', alignItems: 'end', justifyContent: 'center', gap: 30}}>{objects.slice(0, 6).map((item, i) => <div key={`${i}-${item}`} style={{display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12}}><div style={{width: 90, height: `${100 + ((i + 1) * 55) * progress}px`, background: i % 2 ? colors.accent2 : colors.accent, borderRadius: '16px 16px 4px 4px'}}/><span style={{maxWidth: 160, textAlign: 'center', ...textStyle(style, .68)}}>{item}</span></div>)}</div>;
  } else if (template === 'concept_map') {
    content = <div style={{height: '58%', position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center'}}><div style={{position: 'absolute', width: '65%', height: 4, background: colors.accent2, opacity: .55}}/><Card style={style} color={colors.surface} emphasis>{title}</Card><div style={{position: 'absolute', left: '8%', top: '15%'}}><Card style={style} color={colors.surface}>{objects[0] || title}</Card></div><div style={{position: 'absolute', right: '8%', top: '15%'}}><Card style={style} color={colors.surface}>{objects[1] || title}</Card></div><div style={{position: 'absolute', left: '10%', bottom: '4%'}}><Card style={style} color={colors.surface}>{objects[2] || title}</Card></div><div style={{position: 'absolute', right: '10%', bottom: '4%'}}><Card style={style} color={colors.surface}>{objects[3] || title}</Card></div></div>;
  } else {
    content = <div style={{height: '58%', display: 'flex', alignItems: 'center'}}><RelationCards items={objects} style={style}/></div>;
  }
  return <AbsoluteFill style={common}>
    <div style={titleBox}>{title}</div>
    {content}
    {props.shot_class === 'mixed_explanation' && props.digital_human_video_src ? <div style={{position: 'absolute', top: style.pip_top || '53%', right: style.pip_right || '6%', width: style.pip_width || '24%', height: style.pip_height || '34%', borderRadius: Number(style.pip_radius || 28), overflow: 'hidden', border: `5px solid ${colors.surface}`, boxShadow: '0 12px 28px rgba(0,0,0,.2)'}}><Video src={staticFile(props.digital_human_video_src)} muted style={{width: '100%', height: '100%', objectFit: style.pip_object_fit || 'cover'}}/></div> : null}
    <div style={{position: 'absolute', left: '7%', right: '7%', bottom: '5%', height: 8, background: '#E5E0D7', borderRadius: 99}}><div style={{width: `${progress * 100}%`, height: '100%', background: colors.accent, borderRadius: 99}}/></div>
  </AbsoluteFill>;
};

const makeComposition = (template) => (props) => <Scene template={template} props={props}/>;

export const RemotionRoot = () => {
  const entries = [
    ['causal-chain', 'causal_chain'],
    ['contrast-split', 'contrast_split'],
    ['process-steps', 'process_steps'],
    ['hierarchy-layers', 'hierarchy_layers'],
    ['timeline-path', 'timeline_path'],
    ['data-trend', 'data_trend'],
    ['concept-map', 'concept_map'],
  ];
  return <>{entries.map(([id, template]) => <Composition key={id} id={id} component={makeComposition(template)} width={1920} height={1080} fps={30} durationInFrames={90} calculateMetadata={({props}) => ({width: Number(props.width || 1920), height: Number(props.height || 1080), fps: Number(props.fps || 30), durationInFrames: Number(props.duration_frames || 1)})}/>)}</>;
};

export default RemotionRoot;
