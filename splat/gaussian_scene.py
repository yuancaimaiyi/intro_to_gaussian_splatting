import math
import os
from typing import Dict, Tuple

import pycolmap
import torch
from torch import nn
from tqdm import tqdm

from splat.gaussians import Gaussians
from splat.image import GaussianImage
from splat.schema import PreprocessedScene
from splat.utils import (
    compute_2d_covariance,
    compute_gaussian_weight,
    in_view_frustum,
    ndc2Pix,
    read_camera_file,
    read_image_file,
)


class GaussianScene(nn.Module):

    def __init__(
        self,
        colmap_path: str,
        gaussians: Gaussians,
    ) -> None:
        super().__init__()

        camera_dict = read_camera_file(colmap_path)
        image_dict = read_image_file(colmap_path)
        self.images = {}
        for idx in image_dict.keys():
            image = image_dict[idx]
            camera = camera_dict[image.camera_id]
            image = GaussianImage(camera=camera, image=image)
            self.images[idx] = image

        self.gaussians = gaussians

    def render_points_image(self, image_idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Helper function that can be gotten rid of once we know
        implementation is correct
        """
        return self.images[image_idx].project_point_to_camera_perspective_projection(
            self.gaussians.points, self.gaussians.colors
        )

    def get_2d_covariance(
        self, image_idx: int, points: torch.Tensor, covariance_3d: torch.Tensor
    ) -> torch.Tensor:
        """
        Get the 2D covariance matrix for each gaussian
        """
        return compute_2d_covariance(
            points=points,
            W=self.images[image_idx].world2view,
            covariance_3d=covariance_3d,
            tan_fovX=self.images[image_idx].tan_fovX,
            tan_fovY=self.images[image_idx].tan_fovY,
            focal_x=self.images[image_idx].f_x,
            focal_y=self.images[image_idx].f_y,
        )

    def compute_radius(
        self, covariance_2d: torch.Tensor, determinant: torch.Tensor
    ) -> torch.Tensor:
        midpoint = 0.5 * (covariance_2d[:, 0, 0] + covariance_2d[:, 1, 1])
        lambda1 = midpoint + torch.sqrt(midpoint**2 - determinant)
        lambda2 = midpoint - torch.sqrt(midpoint**2 - determinant)
        max_lambda = torch.max(lambda1, lambda2)
        radius = torch.ceil(2.5 * torch.sqrt(max_lambda))
        return radius

    def compute_inverse_covariance(
        self, covariance_2d: torch.Tensor, determinant: torch.Tensor
    ) -> torch.Tensor:
        """
        Compute the inverse covariance matrix
        """
        determinant = torch.clamp(determinant, min=1e-3)
        inverse_covariance = torch.zeros_like(covariance_2d)
        inverse_covariance[:, 0, 0] = covariance_2d[:, 1, 1] / determinant
        inverse_covariance[:, 1, 1] = covariance_2d[:, 0, 0] / determinant
        inverse_covariance[:, 0, 1] = -covariance_2d[:, 0, 1] / determinant
        inverse_covariance[:, 1, 0] = -covariance_2d[:, 1, 0] / determinant
        return inverse_covariance

    def preprocess(self, image_idx: int, tile_size: int = 16) -> None:
        """Preprocesses before rendering begins"""
        in_view = in_view_frustum(
            points=self.gaussians.points,
            view_matrix=self.images[image_idx].world2view,
        )
        covariance_3d = self.gaussians.get_3d_covariance_matrix()[in_view]
        
        points = self.gaussians.points[in_view]
        points_homogeneous = torch.cat([points, torch.ones(points.shape[0], 1)], dim=1)
        points_view = (points_homogeneous @ self.images[image_idx].world2view)[:, :3]
        
        points_ndc = (
            points_homogeneous @ self.images[image_idx].full_proj_transform
        )
        points_ndc = points_ndc[:, :3] / points_ndc[:, 3].unsqueeze(1)
        points_xy = points_ndc[:, :2]
        points_xy[:, 0] = ndc2Pix(points_xy[:, 0], self.images[image_idx].width)
        points_xy[:, 1] = ndc2Pix(points_xy[:, 1], self.images[image_idx].height)

        covariance_2d = self.get_2d_covariance(
            image_idx=image_idx, points=points, covariance_3d=covariance_3d
        )

        # I am not sure what we are exactly doing here
        determinant = (
            covariance_2d[:, 0, 0] * covariance_2d[:, 1, 1]
            - covariance_2d[:, 0, 1] ** 2
        )
        inverse_covariance = self.compute_inverse_covariance(covariance_2d, determinant)
        # now we compute the radius
        radius = self.compute_radius(covariance_2d, determinant)


        min_x = torch.floor(points_xy[:, 0] - radius)
        min_y = torch.floor(points_xy[:, 1] - radius)
        max_x = torch.ceil(points_xy[:, 0] + radius)
        max_y = torch.ceil(points_xy[:, 1] + radius)

        # sort by depth
        colors = self.gaussians.colors[in_view]
        opacity = self.gaussians.opacity[in_view]

        indices_by_depth = torch.argsort(points_view[:, 2])
        points_view = points_view[indices_by_depth]
        colors = colors[indices_by_depth]
        opacity = opacity[indices_by_depth]
        points = points_xy[indices_by_depth]
        covariance_2d = covariance_2d[indices_by_depth]
        inverse_covariance = inverse_covariance[indices_by_depth]
        radius = radius[indices_by_depth]
        points_xy = points_xy[indices_by_depth]
        min_x = min_x[indices_by_depth]
        min_y = min_y[indices_by_depth]
        max_x = max_x[indices_by_depth]
        max_y = max_y[indices_by_depth]

        return PreprocessedScene(
            points=points,
            colors=colors,
            covariance_2d=covariance_2d,
            depths=points_view[:, 2],
            inverse_covariance_2d=inverse_covariance,
            radius=radius,
            points_xy=points_xy,
            min_x=min_x,
            min_y=min_y,
            max_x=max_x,
            max_y=max_y,
            opacity=opacity,
        )

    def render_pixel(
        self,
        pixel_coords: torch.Tensor,
        points_in_tile_mean: torch.Tensor,
        colors: torch.Tensor,
        opacities: torch.Tensor,
        inverse_covariance: torch.Tensor,
        min_weight: float = 0.000001,
    ) -> torch.Tensor:
        total_weight = 1
        pixel_color = torch.zeros((1, 1, 3))
        for point_idx in range(points_in_tile_mean.shape[0]):
            point = points_in_tile_mean[point_idx, :].view(1, 2)
            weight = compute_gaussian_weight(
                pixel_coord=pixel_coords,
                point_mean=point,
                inverse_covariance=inverse_covariance[point_idx],
            )
            alpha = weight * torch.sigmoid(opacities[point_idx])
            test_weight = total_weight * (1-alpha)
            if test_weight < min_weight:
                return pixel_color
            pixel_color += total_weight * alpha * colors[point_idx]
            total_weight = test_weight
        # in case we never reach saturation
        return pixel_color

    def render_tile(
        self,
        x_min: int,
        y_min: int,
        points_in_tile_mean: torch.Tensor,
        colors: torch.Tensor,
        opacities: torch.Tensor,
        inverse_covariance: torch.Tensor,
        tile_size: int = 16,
    ) -> torch.Tensor:
        """Points in tile should be arranged in order of depth"""

        tile = torch.zeros((16, 16, 3))

        for pixel_x in range(x_min, x_min + tile_size):
            for pixel_y in range(y_min, y_min + tile_size):
                tile[pixel_x % tile_size, pixel_y % tile_size] = self.render_pixel(
                    pixel_coords=torch.Tensor([pixel_x, pixel_y]).view(1, 2),
                    points_in_tile_mean=points_in_tile_mean,
                    colors=colors,
                    opacities=opacities,
                    inverse_covariance=inverse_covariance,
                )
        return tile

    def render_image(self, image_idx: int, tile_size: int = 16) -> torch.Tensor:
        """For each tile have to check if the point is in the tile"""
        preprocessed_scene = self.preprocess(image_idx)
        height = self.images[image_idx].height
        width = self.images[image_idx].width

        height = 3200
        width = 4096

        image = torch.zeros((width, height, 3))

        for x_min in tqdm(range(2000, width, tile_size)):
            x_in_tile = (preprocessed_scene.min_x <= x_min + tile_size) & (preprocessed_scene.max_x >= x_min)
            print("x_in_tile", x_in_tile.sum())
            if x_in_tile.sum() == 0:
                continue
            for y_min in range(0, height, tile_size):
                y_in_tile = (preprocessed_scene.min_y <= y_min + tile_size) & (preprocessed_scene.max_y >= y_min)
                points_in_tile = x_in_tile & y_in_tile
                if points_in_tile.sum() == 0:
                    continue
                points_in_tile_mean = preprocessed_scene.points[points_in_tile]
                colors_in_tile = preprocessed_scene.colors[points_in_tile]
                opacities_in_tile = preprocessed_scene.opacity[points_in_tile]
                inverse_covariance_in_tile = preprocessed_scene.inverse_covariance_2d[
                    points_in_tile
                ]
                image[x_min : x_min + tile_size, y_min : y_min + tile_size] = (
                    self.render_tile(
                        x_min=x_min,
                        y_min=y_min,
                        points_in_tile_mean=points_in_tile_mean,
                        colors=colors_in_tile,
                        opacities=opacities_in_tile,
                        inverse_covariance=inverse_covariance_in_tile,
                        tile_size=tile_size,
                    )
                )
        return image
