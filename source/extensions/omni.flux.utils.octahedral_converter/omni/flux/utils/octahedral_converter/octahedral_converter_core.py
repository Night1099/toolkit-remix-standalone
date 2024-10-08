"""
* SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
* SPDX-License-Identifier: Apache-2.0
*
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* https://www.apache.org/licenses/LICENSE-2.0
*
* Unless required by applicable law or agreed to in writing, software
* distributed under the License is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
* See the License for the specific language governing permissions and
* limitations under the License.
"""

from pathlib import Path

import carb
import numpy as np
from PIL import Image


# Converts either OpenGL or DirectX style normal maps to RTX Remix compatible Hemispherical Octahedral maps.
#
# Note that normals pointing in to the surface are not physically possible, and are not supported by RTX Remix.
#   Any images with inward pointing normals will generate a warning and will be flipped to point outwards.
#
# There is a good explanation of DirectX vs OpenGL normal maps at
#   https://www.texturecan.com/post/3/DirectX-vs-OpenGL-Normal-Map/
#
# To use, call this from python as
# `OctahedralConverter.convert_dx_file_to_octahedral("input_dx_normal_map.png", "output_octahedral_map.png")`
#
# To then load these into RTX Remix, you can convert it to a DDS file using
#   https://developer.nvidia.com/nvidia-texture-tools-exporter
#   Use BC5 compression, and the flag --no-mip-gamma-correct
class OctahedralConverter:
    # Convert DirectX style normal maps (green is down)
    @staticmethod
    def convert_dx_file_to_octahedral(dx_path, oth_path):
        if not Path(dx_path).exists():
            carb.log_warn("convert_dx_to_octahedral called on non-existant path: " + dx_path)
            return
        with Image.open(dx_path) as image_file:
            img = np.array(image_file)
            OctahedralConverter._check_for_spherical_normals(dx_path, img)
            img_int = OctahedralConverter.convert_dx_to_octahedral(img)
            Image.fromarray(img_int, "RGB").save(oth_path)

    # Convert OpenGL style normal maps (green is up)
    @staticmethod
    def convert_ogl_file_to_octahedral(ogl_path, oth_path):
        if not Path(ogl_path).exists():
            carb.log_warn("convert_ogl_to_octahedral called on non-existant path: " + ogl_path)
            return
        with Image.open(ogl_path) as image_file:
            img = np.array(image_file)
            OctahedralConverter._check_for_spherical_normals(ogl_path, img)
            img_int = OctahedralConverter.convert_ogl_to_octahedral(img)
            Image.fromarray(img_int, "RGB").save(oth_path)

    @staticmethod
    def convert_dx_to_octahedral(image):
        normals = OctahedralConverter._pixels_to_normals(image)
        octahedrals = OctahedralConverter._convert_to_octahedral(normals)
        return OctahedralConverter._octahedrals_to_pixels(octahedrals)

    @staticmethod
    def convert_ogl_to_octahedral(image):
        dx_image = OctahedralConverter._ogl_to_dx(image)
        return OctahedralConverter.convert_dx_to_octahedral(dx_image)

    @staticmethod
    def _check_for_spherical_normals(original_path, image):
        # Check for blue values below 128.
        mask = image[:, :, 2] < 128
        num_negative = image[mask].shape[0]
        if num_negative > 0:
            carb.log_warn(
                original_path
                + " contained "
                + str(num_negative)
                + " pixels with inward pointing normals (z < 0.0, or b < 128).  RTX Remix only supports hemispherical"
                + " normals, with the normal pointing away from the surface."
            )

        # Mirror the normal to point out from surface.
        image[mask, 2] = 255 - image[mask, 2]

    @staticmethod
    def _pixels_to_normals(image):
        image = image[:, :, 0:3].astype("float32") / 255
        image = image * 2.0 - 1.0
        return image / np.linalg.norm(image, axis=2)[:, :, np.newaxis]

    @staticmethod
    def _octahedrals_to_pixels(octahedrals):
        image = np.floor(octahedrals * 255 + 0.5).astype("uint8")
        return np.pad(image, ((0, 0), (0, 0), (0, 1)), mode="constant")

    @staticmethod
    def _ogl_to_dx(image):
        # flip the g channel to convert to DX style
        image[:, :, (1)] = 255 - image[:, :, (1)]
        return image

    @staticmethod
    def _convert_to_octahedral(image):
        # convert from 3 channel to 2 channel normal map
        # vectorized implementation of hemisphereDirectionToSignedOctahedral from dxvk_rt's packing.glsli

        # p = v.xy / (abs(v.x) + abs(v.y) + abs(v.z));
        abs_values = np.absolute(image)
        snorm_octahedrals = image[:, :, 0:2] / np.expand_dims(abs_values.sum(2), axis=2)
        # Hemisphere normal handling:
        result = snorm_octahedrals.copy()
        result[:, :, 0] = snorm_octahedrals[:, :, 0] + snorm_octahedrals[:, :, 1]
        result[:, :, 1] = snorm_octahedrals[:, :, 0] - snorm_octahedrals[:, :, 1]
        return result * 0.5 + 0.5

        # # Spherical normal handling.  Leaving this in for reference, since it does work.
        # TODO [REMIX-1018] this code will be needed to support Tangent maps
        # snorm_octahedrals = result

        # # snormOctahedral = (v.z >= 0.0) ? p : octWrap(p);
        # needs_wrap_mask = image[:, :, 2] < 0.0
        # # vec2 wrapped = 1.0f - abs(v.yx);
        # snorm_octahedrals[needs_wrap_mask] = -abs_values[needs_wrap_mask, 1::-1] + 1

        # # wrapped.x *= signNotZero(v.x);
        # #   create mask of normals with x < 0 and z < 0
        # needs_xflip_mask = (needs_wrap_mask) & (image[:, :, 0] < 0.0)
        # #   use those masks to flip the x components of snorm_octahedrals
        # snorm_octahedrals[needs_xflip_mask, 0] = -1.0 * snorm_octahedrals[needs_xflip_mask, 0]

        # # wrapped.y *= signNotZero(v.y);
        # #   create mask of normals with y < 0 and z < 0
        # needs_yflip_mask = (needs_wrap_mask) & (image[:, :, 1] < 0.0)
        # #   use those masks to flip the y components of snorm_octahedrals
        # snorm_octahedrals[needs_yflip_mask, 1] = -1.0 * snorm_octahedrals[needs_yflip_mask, 1]

        # return snorm_octahedrals * 0.5 + 0.5
