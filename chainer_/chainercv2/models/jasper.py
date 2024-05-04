"""
    Jasper/DR for ASR, implemented in Chainer.
    Original paper: 'Jasper: An End-to-End Convolutional Neural Acoustic Model,' https://arxiv.org/abs/1904.03288.
"""

__all__ = ['Jasper', 'jasper5x3', 'jasper10x4', 'jasper10x5', 'get_jasper', 'MaskConv1d', 'NemoAudioReader',
           'NemoMelSpecExtractor', 'CtcDecoder']

import os
import numpy as np
import chainer.functions as F
import chainer.links as L
from chainer import Chain
from chainer import initializers
from chainer.initializers.constant import Constant
from functools import partial
from chainer.serializers import load_npz
from .common import DualPathSequential, DualPathParallelConcurent


class NemoAudioReader(object):
    """
    Audio Reader from NVIDIA NEMO toolkit.

    Parameters
    ----------
    desired_audio_sample_rate : int, default 16000
        Desired audio sample rate.
    trunc_value : int or None, default None
        Value to truncate.
    """
    def __init__(self, desired_audio_sample_rate=16000):
        super(NemoAudioReader, self).__init__()
        self.desired_audio_sample_rate = desired_audio_sample_rate

    def read_from_file(self, audio_file_path):
        """
        Read audio from file.

        Parameters
        ----------
        audio_file_path : str
            Path to audio file.

        Returns
        -------
        np.array
            Audio data.
        """
        from soundfile import SoundFile
        with SoundFile(audio_file_path, "r") as data:
            sample_rate = data.samplerate
            audio_data = data.read(dtype="float32")

        audio_data = audio_data.transpose()

        if sample_rate != self.desired_audio_sample_rate:
            from librosa.core import resample as lr_resample
            audio_data = lr_resample(y=audio_data, orig_sr=sample_rate, target_sr=self.desired_audio_sample_rate)
        if audio_data.ndim >= 2:
            audio_data = np.mean(audio_data, axis=1)

        return audio_data

    def read_from_files(self, audio_file_paths):
        """
        Read audios from files.

        Parameters
        ----------
        audio_file_paths : list of str
            Paths to audio files.

        Returns
        -------
        list of np.array
            Audio data.
        """
        assert (type(audio_file_paths) in (list, tuple))

        audio_data_list = []
        for audio_file_path in audio_file_paths:
            audio_data = self.read_from_file(audio_file_path)
            audio_data_list.append(audio_data)
        return audio_data_list


class NemoMelSpecExtractor(Chain):
    """
    Mel-Spectrogram Extractor from NVIDIA NEMO toolkit.

    Parameters
    ----------
    sample_rate : int, default 16000
        Sample rate of the input audio data.
    window_size_sec : float, default 0.02
        Size of window for FFT in seconds.
    window_stride_sec : float, default 0.01
        Stride of window for FFT in seconds.
    n_fft : int, default 512
        Length of FT window.
    n_filters : int, default 64
        Number of Mel spectrogram freq bins.
    preemph : float, default 0.97
        Amount of pre emphasis to add to audio.
    dither : float, default 1.0e-05
        Amount of white-noise dithering.
    """
    def __init__(self,
                 sample_rate=16000,
                 window_size_sec=0.02,
                 window_stride_sec=0.01,
                 n_fft=512,
                 n_filters=64,
                 preemph=0.97,
                 dither=1.0e-05):
        super(NemoMelSpecExtractor, self).__init__()
        self.log_zero_guard_value = 2 ** -24
        win_length = int(window_size_sec * sample_rate)
        self.hop_length = int(window_stride_sec * sample_rate)
        self.n_filters = n_filters

        from scipy import signal as scipy_signal
        from librosa import stft as librosa_stft
        window_arr = scipy_signal.hann(win_length, sym=True)
        self.stft = lambda x: librosa_stft(
            x,
            n_fft=n_fft,
            hop_length=self.hop_length,
            win_length=win_length,
            window=window_arr,
            center=True)

        self.dither = dither
        self.preemph = preemph

        self.pad_align = 16

        from librosa.filters import mel as librosa_mel
        self.fb_arr = librosa_mel(
            sample_rate,
            n_fft,
            n_mels=n_filters,
            fmin=0,
            fmax=(sample_rate / 2))

        with self.init_scope():
            self.window = initializers.generate_array(
                initializer=Constant(0, dtype="float32"),
                shape=window_arr.shape,
                xp=self.xp,
                dtype=np.float32)
            self.register_persistent("window")

            self.fb = initializers.generate_array(
                initializer=Constant(0, dtype="float32"),
                shape=np.expand_dims(self.fb_arr, axis=0).shape,
                xp=self.xp,
                dtype="float32")
            self.register_persistent("fb")

    def __call__(self, xs):
        x_eps = 1e-5

        batch = len(xs)
        x_len = np.zeros((batch,), dtype=np.long)

        ys = []
        for i, xi in enumerate(xs):
            x_len[i] = np.ceil(float(len(xi)) / self.hop_length).astype(np.long)

            if self.dither > 0:
                xi += self.dither * np.random.randn(*xi.shape)

            xi = np.concatenate((xi[:1], xi[1:] - self.preemph * xi[:-1]), axis=0)

            yi = self.stft(xi)
            yi = np.abs(yi)
            yi = np.square(yi)
            yi = np.matmul(self.fb_arr, yi)
            yi = np.log(yi + self.log_zero_guard_value)

            assert (yi.shape[1] != 1)

            yi_mean = yi.mean(axis=1)
            yi_std = yi.std(axis=1)
            yi_std += x_eps

            yi = (yi - np.expand_dims(yi_mean, axis=-1)) / np.expand_dims(yi_std, axis=-1)

            ys.append(yi)

        channels = ys[0].shape[0]
        x_len_max = max([yj.shape[-1] for yj in ys])
        x = np.zeros((batch, channels, x_len_max), dtype=np.float32)
        for i, yi in enumerate(ys):
            x_len_i = x_len[i]
            x[i, :, :x_len_i] = yi[:, :x_len_i]

        pad_rem = x_len_max % self.pad_align
        if pad_rem != 0:
            x = np.pad(x, ((0, 0), (0, 0), (0, self.pad_align - pad_rem)))

        return x, x_len

    def calc_flops(self, x):
        assert (x.shape[0] == 1)
        num_flops = x[0].size
        num_macs = 0
        return num_flops, num_macs


class CtcDecoder(object):
    """
    CTC decoder (to decode a sequence of labels to words).

    Parameters
    ----------
    vocabulary : list of str
        Vocabulary of the dataset.
    """
    def __init__(self,
                 vocabulary):
        super().__init__()
        self.blank_id = len(vocabulary)
        self.labels_map = dict([(i, vocabulary[i]) for i in range(len(vocabulary))])

    def __call__(self,
                 predictions):
        """
        Decode a sequence of labels to words.

        Parameters
        ----------
        predictions : np.array of int or list of list of int
            Tensor with predicted labels.

        Returns
        -------
        list of str
            Words.
        """
        hypotheses = []
        for prediction in predictions:
            decoded_prediction = []
            previous = self.blank_id
            for p in prediction:
                if (p != previous or previous == self.blank_id) and p != self.blank_id:
                    decoded_prediction.append(p)
                previous = p
            hypothesis = "".join([self.labels_map[c] for c in decoded_prediction])
            hypotheses.append(hypothesis)
        return hypotheses


def conv1d1(in_channels,
            out_channels,
            stride=1,
            groups=1,
            use_bias=False,
            **kwargs):
    """
    1-dim kernel version of the 1D convolution layer.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    stride : int, default 1
        Stride of the convolution.
    groups : int, default 1
        Number of groups.
    use_bias : bool, default False
        Whether the layer uses a bias vector.
    """
    return L.Convolution1D(
        in_channels=in_channels,
        out_channels=out_channels,
        ksize=1,
        stride=stride,
        nobias=(not use_bias),
        groups=groups,
        **kwargs)


class MaskConv1d(L.Convolution1D):
    """
    Masked 1D convolution block.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    ksize : int or tuple/list of 1 int
        Convolution window size.
    stride : int or tuple/list of 1 int
        Stride of the convolution.
    pad : int or tuple/list of 1 int, default 0
        Padding value for convolution layer.
    dilate : int or tuple/list of 1 int, default 1
        Dilation value for convolution layer.
    groups : int, default 1
        Number of groups.
    use_bias : bool, default False
        Whether the layer uses a bias vector.
    use_mask : bool, default True
        Whether to use mask.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 ksize,
                 stride,
                 pad=0,
                 dilate=1,
                 groups=1,
                 use_bias=False,
                 use_mask=True,
                 **kwargs):
        super(MaskConv1d, self).__init__(
            in_channels=in_channels,
            out_channels=out_channels,
            ksize=ksize,
            stride=stride,
            pad=pad,
            nobias=(not use_bias),
            dilate=dilate,
            groups=groups,
            **kwargs)
        self.use_mask = use_mask
        if self.use_mask:
            self.stride0 = stride[0] if isinstance(stride, (list, tuple)) else stride
            self.pad0 = pad[0] if isinstance(pad, (list, tuple)) else pad

    def __call__(self, x, x_len):
        if self.use_mask:
            mask = (F.broadcast_to(self.xp.arange(x.shape[2]), x.shape).array <
                    F.expand_dims(F.expand_dims(x_len, -1), -1).array)
            x *= mask
            x_len = (x_len + 2 * self.pad0 - self.dilate[0] * (self.ksize[0] - 1) - 1) // self.stride0 + 1
        x = F.convolution_1d(
            x=x,
            W=self.W,
            b=self.b,
            stride=self.stride,
            pad=self.pad,
            dilate=self.dilate,
            groups=self.groups)
        return x, x_len


def mask_conv1d1(in_channels,
                 out_channels,
                 stride=1,
                 groups=1,
                 use_bias=False,
                 **kwargs):
    """
    Masked 1-dim kernel version of the 1D convolution layer.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    stride : int, default 1
        Stride of the convolution.
    groups : int, default 1
        Number of groups.
    use_bias : bool, default False
        Whether the layer uses a bias vector.
    """
    return MaskConv1d(
        in_channels=in_channels,
        out_channels=out_channels,
        ksize=1,
        stride=stride,
        groups=groups,
        use_bias=use_bias,
        **kwargs)


class MaskConvBlock1d(Chain):
    """
    Masked 1D convolution block with batch normalization, activation, and dropout.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    ksize : int
        Convolution window size.
    stride : int
        Stride of the convolution.
    pad : int
        Padding value for convolution layer.
    dilate : int, default 1
        Dilation value for convolution layer.
    groups : int, default 1
        Number of groups.
    use_bias : bool, default False
        Whether the layer uses a bias vector.
    use_bn : bool, default True
        Whether to use BatchNorm layer.
    bn_eps : float, default 1e-5
        Small float added to variance in Batch norm.
    activation : function or str or None, default nn.Activation('relu')
        Activation function or name of activation function.
    dropout_rate : float, default 0.0
        Parameter of Dropout layer. Faction of the input units to drop.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 ksize,
                 stride,
                 pad,
                 dilate=1,
                 groups=1,
                 use_bias=False,
                 use_bn=True,
                 bn_eps=1e-5,
                 activation=(lambda: F.relu),
                 dropout_rate=0.0,
                 **kwargs):
        super(MaskConvBlock1d, self).__init__(**kwargs)
        self.activate = (activation is not None)
        self.use_bn = use_bn
        self.use_dropout = (dropout_rate != 0.0)

        with self.init_scope():
            self.conv = MaskConv1d(
                in_channels=in_channels,
                out_channels=out_channels,
                ksize=ksize,
                stride=stride,
                pad=pad,
                dilate=dilate,
                groups=groups,
                use_bias=use_bias)
            if self.use_bn:
                self.bn = L.BatchNormalization(
                    size=out_channels,
                    eps=bn_eps)
            if self.activate:
                self.activ = activation()
            if self.use_dropout:
                self.dropout = partial(
                    F.dropout,
                    ratio=dropout_rate)

    def __call__(self, x, x_len):
        x, x_len = self.conv(x, x_len)
        if self.use_bn:
            x = self.bn(x)
        if self.activate:
            x = self.activ(x)
        if self.use_dropout:
            x = self.dropout(x)
        return x, x_len


def mask_conv1d1_block(in_channels,
                       out_channels,
                       stride=1,
                       pad=0,
                       **kwargs):
    """
    1-dim kernel version of the masked 1D convolution block.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    stride : int, default 1
        Stride of the convolution.
    pad : int, default 0
        Padding value for convolution layer.
    """
    return MaskConvBlock1d(
        in_channels=in_channels,
        out_channels=out_channels,
        ksize=1,
        stride=stride,
        pad=pad,
        **kwargs)


class ChannelShuffle1d(Chain):
    """
    1D version of the channel shuffle layer.

    Parameters
    ----------
    channels : int
        Number of channels.
    groups : int
        Number of groups.
    """
    def __init__(self,
                 channels,
                 groups,
                 **kwargs):
        super(ChannelShuffle1d, self).__init__(**kwargs)
        assert (channels % groups == 0)
        self.groups = groups

    def __call__(self, x):
        batch, channels, seq_len = x.shape
        channels_per_group = channels // self.groups
        x = F.reshape(x, shape=(batch, self.groups, channels_per_group, seq_len))
        x = F.swapaxes(x, axis1=1, axis2=2)
        x = F.reshape(x, shape=(batch, channels, seq_len))
        return x

    def __repr__(self):
        s = "{name}(groups={groups})"
        return s.format(
            name=self.__class__.__name__,
            groups=self.groups)


class DwsConvBlock1d(Chain):
    """
    Depthwise version of the 1D standard convolution block with batch normalization, activation, dropout, and channel
    shuffle.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    ksize : int
        Convolution window size.
    stride : int
        Stride of the convolution.
    pad : int
        Padding value for convolution layer.
    dilate : int, default 1
        Dilation value for convolution layer.
    groups : int, default 1
        Number of groups.
    use_bias : bool, default False
        Whether the layer uses a bias vector.
    use_bn : bool, default True
        Whether to use BatchNorm layer.
    bn_eps : float, default 1e-5
        Small float added to variance in Batch norm.
    activation : function or str or None, default nn.Activation('relu')
        Activation function or name of activation function.
    dropout_rate : float, default 0.0
        Parameter of Dropout layer. Faction of the input units to drop.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 ksize,
                 stride,
                 pad,
                 dilate=1,
                 groups=1,
                 use_bias=False,
                 use_bn=True,
                 bn_eps=1e-5,
                 activation=(lambda: F.relu),
                 dropout_rate=0.0,
                 **kwargs):
        super(DwsConvBlock1d, self).__init__(**kwargs)
        self.activate = (activation is not None)
        self.use_bn = use_bn
        self.use_dropout = (dropout_rate != 0.0)
        self.use_channel_shuffle = (groups > 1)

        with self.init_scope():
            self.dw_conv = MaskConv1d(
                in_channels=in_channels,
                out_channels=in_channels,
                ksize=ksize,
                stride=stride,
                pad=pad,
                dilate=dilate,
                groups=in_channels,
                use_bias=use_bias)
            self.pw_conv = mask_conv1d1(
                in_channels=in_channels,
                out_channels=out_channels,
                groups=groups,
                use_bias=use_bias)
            if self.use_channel_shuffle:
                self.shuffle = ChannelShuffle1d(
                    channels=out_channels,
                    groups=groups)
            if self.use_bn:
                self.bn = L.BatchNormalization(
                    size=out_channels,
                    eps=bn_eps)
            if self.activate:
                self.activ = activation()
            if self.use_dropout:
                self.dropout = partial(
                    F.dropout,
                    ratio=dropout_rate)

    def __call__(self, x, x_len):
        x, x_len = self.dw_conv(x, x_len)
        x, x_len = self.pw_conv(x, x_len)
        if self.use_channel_shuffle:
            x = self.shuffle(x)
        if self.use_bn:
            x = self.bn(x)
        if self.activate:
            x = self.activ(x)
        if self.use_dropout:
            x = self.dropout(x)
        return x, x_len


class JasperUnit(Chain):
    """
    Jasper unit with residual connection.

    Parameters
    ----------
    in_channels : int or list of int
        Number of input channels.
    out_channels : int
        Number of output channels.
    ksize : int
        Convolution window size.
    bn_eps : float
        Small float added to variance in Batch norm.
    dropout_rate : float
        Parameter of Dropout layer. Faction of the input units to drop.
    repeat : int
        Count of body convolution blocks.
    use_dw : bool
        Whether to use depthwise block.
    use_dr : bool
        Whether to use dense residual scheme.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 ksize,
                 bn_eps,
                 dropout_rate,
                 repeat,
                 use_dw,
                 use_dr,
                 **kwargs):
        super(JasperUnit, self).__init__(**kwargs)
        self.use_dropout = (dropout_rate != 0.0)
        self.use_dr = use_dr
        block_class = DwsConvBlock1d if use_dw else MaskConvBlock1d

        with self.init_scope():
            if self.use_dr:
                self.identity_block = DualPathParallelConcurent()
                with self.identity_block.init_scope():
                    for i, dense_in_channels_i in enumerate(in_channels):
                        setattr(self.identity_block, "block{}".format(i + 1), mask_conv1d1_block(
                            in_channels=dense_in_channels_i,
                            out_channels=out_channels,
                            bn_eps=bn_eps,
                            dropout_rate=0.0,
                            activation=None))
                in_channels = in_channels[-1]
            else:
                self.identity_block = mask_conv1d1_block(
                    in_channels=in_channels,
                    out_channels=out_channels,
                    bn_eps=bn_eps,
                    dropout_rate=0.0,
                    activation=None)

            self.body = DualPathSequential()
            with self.body.init_scope():
                for i in range(repeat):
                    activation = (lambda: F.relu) if i < repeat - 1 else None
                    dropout_rate_i = dropout_rate if i < repeat - 1 else 0.0
                    setattr(self.body, "block{}".format(i + 1), block_class(
                        in_channels=in_channels,
                        out_channels=out_channels,
                        ksize=ksize,
                        stride=1,
                        pad=(ksize // 2),
                        bn_eps=bn_eps,
                        dropout_rate=dropout_rate_i,
                        activation=activation))
                    in_channels = out_channels

            self.activ = F.relu
            if self.use_dropout:
                self.dropout = partial(
                    F.dropout,
                    ratio=dropout_rate)

    def __call__(self, x, x_len):
        if self.use_dr:
            x_len, y, y_len = x_len if type(x_len) is tuple else (x_len, None, None)
            y = [x] if y is None else y + [x]
            y_len = [x_len] if y_len is None else y_len + [x_len]
            identity, _ = self.identity_block(y, y_len)
            identity = F.stack(tuple(identity), axis=1)
            identity = F.sum(identity, axis=1)
        else:
            identity, _ = self.identity_block(x, x_len)

        x, x_len = self.body(x, x_len)
        x = x + identity
        x = self.activ(x)
        if self.use_dropout:
            x = self.dropout(x)

        if self.use_dr:
            return x, (x_len, y, y_len)
        else:
            return x, x_len


class JasperFinalBlock(Chain):
    """
    Jasper specific final block.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    channels : list of int
        Number of output channels for each block.
    ksizes : list of int
        Kernel sizes for each block.
    bn_eps : float
        Small float added to variance in Batch norm.
    dropout_rates : list of int
        Dropout rates for each block.
    use_dw : bool
        Whether to use depthwise block.
    use_dr : bool
        Whether to use dense residual scheme.
    """
    def __init__(self,
                 in_channels,
                 channels,
                 ksizes,
                 bn_eps,
                 dropout_rates,
                 use_dw,
                 use_dr,
                 **kwargs):
        super(JasperFinalBlock, self).__init__(**kwargs)
        self.use_dr = use_dr
        conv1_class = DwsConvBlock1d if use_dw else MaskConvBlock1d

        with self.init_scope():
            self.conv1 = conv1_class(
                in_channels=in_channels,
                out_channels=channels[-2],
                ksize=ksizes[-2],
                stride=1,
                pad=(2 * ksizes[-2] // 2 - 1),
                dilate=2,
                bn_eps=bn_eps,
                dropout_rate=dropout_rates[-2])
            self.conv2 = MaskConvBlock1d(
                in_channels=channels[-2],
                out_channels=channels[-1],
                ksize=ksizes[-1],
                stride=1,
                pad=(ksizes[-1] // 2),
                bn_eps=bn_eps,
                dropout_rate=dropout_rates[-1])

    def __call__(self, x, x_len):
        if self.use_dr:
            x_len = x_len[0]
        x, x_len = self.conv1(x, x_len)
        x, x_len = self.conv2(x, x_len)
        return x, x_len


class Jasper(Chain):
    """
    Jasper/DR/QuartzNet model from 'Jasper: An End-to-End Convolutional Neural Acoustic Model,'
    https://arxiv.org/abs/1904.03288.

    Parameters
    ----------
    channels : list of int
        Number of output channels for each unit and initial/final block.
    ksizes : list of int
        Kernel sizes for each unit and initial/final block.
    bn_eps : float
        Small float added to variance in Batch norm.
    dropout_rates : list of int
        Dropout rates for each unit and initial/final block.
    repeat : int
        Count of body convolution blocks.
    use_dw : bool
        Whether to use depthwise block.
    use_dr : bool
        Whether to use dense residual scheme.
    from_audio : bool, default True
        Whether to treat input as audio instead of Mel-specs.
    dither : float, default 0.0
        Amount of white-noise dithering.
    return_text : bool, default False
        Whether to return text instead of logits.
    vocabulary : list of str or None, default None
        Vocabulary of the dataset.
    in_channels : int, default 64
        Number of input channels (audio features).
    classes : int, default 29
        Number of classification classes (number of graphemes).
    """
    def __init__(self,
                 channels,
                 ksizes,
                 bn_eps,
                 dropout_rates,
                 repeat,
                 use_dw,
                 use_dr,
                 from_audio=True,
                 dither=0.0,
                 return_text=False,
                 vocabulary=None,
                 in_channels=64,
                 classes=29,
                 **kwargs):
        super(Jasper, self).__init__(**kwargs)
        self.in_size = in_channels
        self.classes = classes
        self.vocabulary = vocabulary
        self.from_audio = from_audio
        self.return_text = return_text

        with self.init_scope():
            if self.from_audio:
                self.preprocessor = NemoMelSpecExtractor(dither=dither)

            self.features = DualPathSequential()
            with self.features.init_scope():
                init_block_class = DwsConvBlock1d if use_dw else MaskConvBlock1d
                setattr(self.features, "init_block", init_block_class(
                    in_channels=in_channels,
                    out_channels=channels[0],
                    ksize=ksizes[0],
                    stride=2,
                    pad=(ksizes[0] // 2),
                    bn_eps=bn_eps,
                    dropout_rate=dropout_rates[0]))
                in_channels = channels[0]
                in_channels_list = []
                for i, (out_channels, ksize, dropout_rate) in\
                        enumerate(zip(channels[1:-2], ksizes[1:-2], dropout_rates[1:-2])):
                    in_channels_list += [in_channels]
                    setattr(self.features, "unit{}".format(i + 1), JasperUnit(
                        in_channels=(in_channels_list if use_dr else in_channels),
                        out_channels=out_channels,
                        ksize=ksize,
                        bn_eps=bn_eps,
                        dropout_rate=dropout_rate,
                        repeat=repeat,
                        use_dw=use_dw,
                        use_dr=use_dr))
                    in_channels = out_channels
                setattr(self.features, "final_block", JasperFinalBlock(
                    in_channels=in_channels,
                    channels=channels,
                    ksizes=ksizes,
                    bn_eps=bn_eps,
                    dropout_rates=dropout_rates,
                    use_dw=use_dw,
                    use_dr=use_dr))
                in_channels = channels[-1]

            self.output = conv1d1(
                in_channels=in_channels,
                out_channels=classes,
                use_bias=True)

            if self.return_text:
                self.ctc_decoder = CtcDecoder(vocabulary=vocabulary)

    def __call__(self, x, x_len=None):
        if x_len is None:
            assert (type(x) in (list, tuple))
            x, x_len = x

        if self.from_audio:
            x, x_len = self.preprocessor(x if type(x) is np.ndarray else x.array)

        x, x_len = self.features(x, x_len)
        x = self.output(x)

        if self.return_text:
            greedy_predictions = x.swapaxes(1, 2).log_softmax(dim=-1).argmax(dim=-1, keepdim=False).asnumpy()
            return self.ctc_decoder(greedy_predictions)
        else:
            return x, x_len


def get_jasper(version,
               use_dw=False,
               use_dr=False,
               bn_eps=1e-3,
               vocabulary=None,
               model_name=None,
               pretrained=False,
               root=os.path.join("~", ".chainer", "models"),
               **kwargs):
    """
    Create Jasper/DR/QuartzNet model with specific parameters.

    Parameters
    ----------
    version : tuple of str
        Model type and configuration.
    use_dw : bool, default False
        Whether to use depthwise block.
    use_dr : bool, default False
        Whether to use dense residual scheme.
    bn_eps : float, default 1e-3
        Small float added to variance in Batch norm.
    vocabulary : list of str or None, default None
        Vocabulary of the dataset.
    model_name : str or None, default None
        Model name for loading pretrained model.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    import numpy as np

    blocks, repeat = tuple(map(int, version[1].split("x")))
    main_stage_repeat = blocks // 5

    model_type = version[0]
    if model_type == "jasper":
        channels_per_stage = [256, 256, 384, 512, 640, 768, 896, 1024]
        ksizes_per_stage = [11, 11, 13, 17, 21, 25, 29, 1]
        dropout_rates_per_stage = [0.2, 0.2, 0.2, 0.2, 0.3, 0.3, 0.4, 0.4]
    elif model_type == "quartznet":
        channels_per_stage = [256, 256, 256, 512, 512, 512, 512, 1024]
        ksizes_per_stage = [33, 33, 39, 51, 63, 75, 87, 1]
        dropout_rates_per_stage = [0.0] * 8
    else:
        raise ValueError("Unsupported Jasper family model type: {}".format(model_type))

    stage_repeat = np.full((8,), 1)
    stage_repeat[1:-2] *= main_stage_repeat
    channels = sum([[a] * r for (a, r) in zip(channels_per_stage, stage_repeat)], [])
    ksizes = sum([[a] * r for (a, r) in zip(ksizes_per_stage, stage_repeat)], [])
    dropout_rates = sum([[a] * r for (a, r) in zip(dropout_rates_per_stage, stage_repeat)], [])

    net = Jasper(
        channels=channels,
        ksizes=ksizes,
        bn_eps=bn_eps,
        dropout_rates=dropout_rates,
        repeat=repeat,
        use_dw=use_dw,
        use_dr=use_dr,
        vocabulary=vocabulary,
        **kwargs)

    if pretrained:
        if (model_name is None) or (not model_name):
            raise ValueError("Parameter `model_name` should be properly initialized for loading pretrained model.")
        from .model_store import get_model_file
        load_npz(
            file=get_model_file(
                model_name=model_name,
                local_model_store_dir_path=root),
            obj=net)

    return net


def jasper5x3(**kwargs):
    """
    Jasper 5x3 model from 'Jasper: An End-to-End Convolutional Neural Acoustic Model,'
    https://arxiv.org/abs/1904.03288.

    Parameters
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_jasper(version=("jasper", "5x3"), model_name="jasper5x3", **kwargs)


def jasper10x4(**kwargs):
    """
    Jasper 10x4 model from 'Jasper: An End-to-End Convolutional Neural Acoustic Model,'
    https://arxiv.org/abs/1904.03288.

    Parameters
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_jasper(version=("jasper", "10x4"), model_name="jasper10x4", **kwargs)


def jasper10x5(**kwargs):
    """
    Jasper 10x5 model from 'Jasper: An End-to-End Convolutional Neural Acoustic Model,'
    https://arxiv.org/abs/1904.03288.

    Parameters
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_jasper(version=("jasper", "10x5"), model_name="jasper10x5", **kwargs)


def _test():
    import chainer

    chainer.global_config.train = False

    pretrained = False
    from_audio = True
    audio_features = 64
    classes = 29

    models = [
        jasper5x3,
        jasper10x4,
        jasper10x5,
    ]

    for model in models:

        net = model(
            in_channels=audio_features,
            classes=classes,
            from_audio=from_audio,
            pretrained=pretrained)

        weight_count = net.count_params()
        print("m={}, {}".format(model.__name__, weight_count))
        # assert (model != jasper5x3 or weight_count == 107681053)
        # assert (model != jasper10x4 or weight_count == 261393693)
        # assert (model != jasper10x5 or weight_count == 322286877)

        batch = 3
        aud_scale = 640 if from_audio else 1
        seq_len = np.random.randint(150, 250, batch) * aud_scale
        seq_len_max = seq_len.max() + 2
        x_shape = (batch, seq_len_max) if from_audio else (batch, audio_features, seq_len_max)
        x = np.random.rand(*x_shape).astype(np.float32)
        x_len = seq_len.astype(np.long)

        y, y_len = net(x, x_len)

        assert (y.shape[:2] == (batch, net.classes))
        if from_audio:
            assert (y.shape[2] in range(seq_len_max // aud_scale * 2, seq_len_max // aud_scale * 2 + 9))
        else:
            assert (y.shape[2] in [seq_len_max // 2, seq_len_max // 2 + 1])


if __name__ == "__main__":
    _test()
