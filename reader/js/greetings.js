/* Liubai — time-based greetings. */

const GREETING_POOLS = {
  morning: [   // 6:00–10:30
    '早上好。今天想读哪本书？',
    '早安。泡杯茶，翻开一页吧。',
    '晨光正好，适合读书。想看点什么？',
    '新的一天开始了，今天读什么？',
    '早啊。书架上的书在等你了。',
  ],
  noon: [      // 10:30–13:30
    '中午好。午饭后来一段？',
    '午间时分，读段文字消消食。',
    '正午阳光正好，窝在沙发上读会儿书吧。',
    '吃过午饭了吗？翻开书就是最好的休息。',
  ],
  afternoon: [ // 13:30–16:30
    '下午好。来杯咖啡，读段书。',
    '下午的时光最适合沉浸式阅读。',
    '倦意袭来的时候，不妨读一段提提神。',
    '下午好。上次读到哪儿了？',
  ],
  evening: [   // 16:30–18:30
    '傍晚了。天色渐暗，正是读书的好时候。',
    '黄昏时分，点盏灯，翻几页。',
    '一天将尽，读段文字收个尾吧。',
    '傍晚好。窗外天色转暗，书里的世界刚刚亮起来。',
  ],
  night: [     // 18:30–22:00
    '晚上好。夜读时光，安静又惬意。',
    '夜深了，翻开书就是另一个世界。',
    '晚上的时间最适合慢慢读，细细想。',
    '晚上好。今天的阅读计划完成了吗？',
  ],
  midnight: [  // 22:00–24:00
    '夜深了。不过夜猫子最适合这个时间读书。',
    '午夜时分，万籁俱寂，只有你和书。',
    '这么晚还在，是想读一段再睡吗？',
    '深夜的书架泛着暖光，挑一本陪你再待会儿。',
  ],
  wee: [       // 0:00–4:00
    '还在呢。这个点读书，别有一番滋味。',
    '凌晨了。不过有些书，就是该在夜深人静时读。',
    '失眠的夜晚，不妨让书陪你。',
    '夜深到极致，连文字都变得更清晰了。',
    '还没睡吗？那就再读一段吧。',
  ],
  dawn: [      // 4:00–6:00
    '快天亮了。失眠还是早起？',
    '天快亮了，读一段等待日出吧。',
    '黎明前的时光，安静得只听得见翻书声。',
  ],
};

export function pickGreeting() {
  const now = new Date();
  const t = now.getHours() * 60 + now.getMinutes();

  let pool;
  if (t >= 360 && t < 630) pool = GREETING_POOLS.morning;       // 6:00–10:30
  else if (t >= 630 && t < 810) pool = GREETING_POOLS.noon;      // 10:30–13:30
  else if (t >= 810 && t < 990) pool = GREETING_POOLS.afternoon; // 13:30–16:30
  else if (t >= 990 && t < 1110) pool = GREETING_POOLS.evening;  // 16:30–18:30
  else if (t >= 1110 && t < 1320) pool = GREETING_POOLS.night;   // 18:30–22:00
  else if (t >= 1320) pool = GREETING_POOLS.midnight;            // 22:00–24:00
  else if (t < 240) pool = GREETING_POOLS.wee;                   // 0:00–4:00
  else pool = GREETING_POOLS.dawn;                                // 4:00–6:00

  return pool[Math.floor(Math.random() * pool.length)];
}

export function getWelcomeMessage() {
  return pickGreeting();
}
